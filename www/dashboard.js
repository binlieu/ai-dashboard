import './vendor/video-stream.js';

const find = selector => document.querySelector(selector);
const dashboard = find('.dashboard');
const grid = find('#camera-grid');
const clock = find('#clock');
const summary = find('#summary');
const setupDialog = find('#setup-dialog');
const setupButton = find('#setup-button');
const addForm = find('#add-camera-form');
const macInput = find('#camera-mac');
const nameInput = find('#camera-name');
const layoutButtons = [...document.querySelectorAll('.layout-options button')];
const mobile = matchMedia('(max-width: 699px)');
const layouts = ['auto', '2x1', '2x2', '3x3', '4x4'];
const tiles = new Map();
const rows = new Map();
const state = {
    cameras: [], max: 16, loaded: false, layout: 'auto', page: 0, pageCount: 1,
    visible: [], busy: false, videoChangePending: false, restartUntil: 0,
    cameraRevision: 0, layoutRevision: 0, layoutDirty: false,
};
let loadTimer = 0;
let loadPromise = null;
let restartTimer = 0;
let layoutSaving = false;
let queuedLayout = null;

class ApiError extends Error {
    constructor(message, code = '', status = 0) {
        super(message);
        this.code = code;
        this.status = status;
    }
}

async function request(path, {method = 'GET', body, image = false, signal} = {}) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    const timer = setTimeout(abort, method === 'GET' ? 15000 : 45000);
    if (signal) {
        signal.addEventListener('abort', abort, {once: true});
        if (signal.aborted) abort();
    }
    const options = {method, cache: 'no-store', credentials: 'same-origin', signal: controller.signal};
    if (body !== undefined) {
        options.headers = {'Content-Type': 'application/json'};
        options.body = JSON.stringify(body);
    }
    try {
        const response = await fetch(path, options);
        if (!response.ok) {
            let error = {};
            try { error = await response.json(); } catch { /* A gate error can be plain text. */ }
            throw new ApiError(typeof error.message === 'string' ? error.message : `Request failed (${response.status}).`, error.error, response.status);
        }
        if (response.status === 204) return null;
        return image ? await response.blob() : await response.json();
    } catch (error) {
        if (error instanceof ApiError) throw error;
        throw new ApiError('Dashboard server is not responding.');
    } finally {
        clearTimeout(timer);
        if (signal) signal.removeEventListener('abort', abort);
    }
}

function normalizeMac(value) {
    let text = value.trim();
    if (/^[0-9a-f]{12}$/i.test(text)) text = text.match(/.{2}/g).join(':');
    if (!/^([0-9a-f]{1,2}:){5}[0-9a-f]{1,2}$/i.test(text) &&
        !/^([0-9a-f]{1,2}-){5}[0-9a-f]{1,2}$/i.test(text)) return null;
    return text.replace(/-/g, ':').split(':').map(octet => octet.toLowerCase().padStart(2, '0')).join(':');
}

class CameraTile {
    constructor(camera) {
        this.camera = camera;
        this.state = 'Paused';
        this.visible = false;
        this.destroyed = false;
        this.player = null;
        this.closingSocket = null;
        this.closeListener = null;
        this.lastReconnectAt = -Infinity;
        this.snapshotController = null;
        this.objectURL = null;
        this.cleanupTimer = 0;
        this.card = find('#camera-template').content.firstElementChild.cloneNode(true);
        this.tile = this.card.querySelector('.camera-tile');
        this.status = this.card.querySelector('.status');
        this.lastFrame = this.card.querySelector('.last-frame');
        this.snapshotButton = this.card.querySelector('.snapshot');
        this.fullscreenButton = this.card.querySelector('.fullscreen');
        this.notice = this.card.querySelector('.notice');
        this.snapshotButton.addEventListener('click', () => this.snapshot());
        this.fullscreenButton.addEventListener('click', () => this.fullscreen());
        this.update(camera);
    }

    update(camera) {
        const wasAvailable = this.camera.available;
        this.camera = camera;
        this.card.querySelector('.camera-name').textContent = camera.name;
        this.card.querySelector('.tile-label').textContent = camera.name;
        this.tile.setAttribute('aria-label', `${camera.name} video`);
        this.snapshotButton.setAttribute('aria-label', `Snapshot: ${camera.name}`);
        this.updateFullscreenLabel();
        this.card.querySelector('.unavailable-message').hidden = camera.available;
        this.card.querySelector('.controls').hidden = !camera.available;
        if (!camera.available) {
            this.disconnectPlayer();
            this.showStatus('Unavailable');
        } else if (!wasAvailable && this.visible) {
            this.resetTracking();
        }
    }

    showStatus(label) {
        this.state = label;
        if (this.status.textContent !== label) {
            this.status.textContent = label;
            this.status.dataset.state = label.toLowerCase();
        }
        this.lastFrame.hidden = label === 'Live' || label === 'Unavailable' || label === 'Paused';
    }

    resetTracking() {
        this.watchStartedAt = performance.now();
        this.lastProgressAt = null;
        this.previousTime = this.player?.video?.currentTime ?? 0;
        this.lastFrame.textContent = 'last frame: waiting';
        this.showStatus(this.camera.available ? 'Connecting' : 'Unavailable');
    }

    clearPlayerTimers() {
        if (!this.player) return;
        for (const key of ['disconnectTID', 'reconnectTID']) {
            clearTimeout(this.player[key]);
            this.player[key] = 0;
        }
    }

    connectPlayer() {
        if (this.destroyed || !this.visible || !this.camera.available || document.hidden || this.closingSocket) return;
        if (!this.player) {
            this.player = document.createElement('video-stream');
            this.player.mode = 'mse';
            this.player.media = 'video';
        }
        this.clearPlayerTimers();
        if (!this.player.isConnected) this.card.querySelector('.player-container').appendChild(this.player);
        const video = this.player.video;
        video.controls = false;
        video.muted = true;
        video.defaultMuted = true;
        video.autoplay = true;
        if (!this.player.wsURL) {
            this.player.src = new URL(`api/ws?src=${encodeURIComponent(this.camera.id)}`, location.href);
        } else {
            this.player.onconnect();
        }
    }

    disconnectPlayer() {
        if (!this.player) return;
        this.clearPlayerTimers();
        const socket = this.player.ws;
        if (socket && !this.closingSocket) {
            this.closingSocket = socket;
            this.closeListener = () => {
                this.closingSocket = null;
                this.closeListener = null;
                this.connectPlayer();
            };
            socket.addEventListener('close', this.closeListener, {once: true});
        }
        this.player.ondisconnect();
        // Detached players cannot reconnect through the vendor's document visibility listener.
        this.player.remove();
        this.clearPlayerTimers();
    }

    setVisible(visible) {
        if (this.visible === visible) {
            if (visible && this.camera.available && !this.player?.isConnected) this.connectPlayer();
            return;
        }
        this.visible = visible;
        if (visible) {
            this.resetTracking();
            this.connectPlayer();
        } else {
            this.disconnectPlayer();
            this.card.remove();
            this.showStatus(this.camera.available ? 'Paused' : 'Unavailable');
        }
    }

    updatePlaybackStatus() {
        if (this.destroyed || !this.visible || !this.camera.available || !this.player || document.hidden) return;
        const now = performance.now();
        const currentTime = this.player.video.currentTime;
        if (currentTime > this.previousTime) this.lastProgressAt = now;
        this.previousTime = currentTime;
        const stalledFor = now - (this.lastProgressAt ?? this.watchStartedAt);
        if (this.lastProgressAt !== null && stalledFor < 5000) this.showStatus('Live');
        else if (state.videoChangePending || now < state.restartUntil) this.showStatus('Connecting');
        else if (stalledFor >= 30000) this.showStatus('Offline');
        else if (stalledFor >= 5000) this.showStatus('Reconnecting');
        else this.showStatus('Connecting');
        this.lastFrame.textContent = this.lastProgressAt === null
            ? 'last frame: waiting' : `last frame: ${Math.floor(stalledFor / 1000)}s ago`;
        if (stalledFor > 30000 && !state.videoChangePending && now >= state.restartUntil &&
            this.player.ws?.readyState === WebSocket.OPEN && now - this.lastReconnectAt >= 30000) {
            this.lastReconnectAt = now;
            this.disconnectPlayer();
            // The old close handler reconnects only if this tile is still visible.
            this.previousTime = this.player.video.currentTime;
        }
    }

    visibilityChanged() {
        if (!this.visible || !this.camera.available) return;
        this.showStatus('Connecting');
        if (document.hidden) return;
        this.resetTracking();
        if (!this.player || !this.player.isConnected) this.connectPlayer();
    }

    snapshotFilename(now) {
        const pad = value => String(value).padStart(2, '0');
        const date = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`;
        const time = `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
        const slug = this.camera.name.normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
            .toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || this.camera.id;
        return `${slug}-${date}-${time}.jpg`;
    }

    releaseSnapshot() {
        clearTimeout(this.cleanupTimer);
        if (this.objectURL) URL.revokeObjectURL(this.objectURL);
        this.objectURL = null;
    }

    async snapshot() {
        this.snapshotButton.disabled = true;
        this.notice.textContent = '';
        this.snapshotController = new AbortController();
        try {
            const image = await request(`api/frame.jpeg?src=${encodeURIComponent(this.camera.id)}`, {
                image: true, signal: this.snapshotController.signal,
            });
            if (this.destroyed) return;
            if (image.type.split(';')[0] !== 'image/jpeg' || !image.size) throw new ApiError('Snapshot unavailable. Try again.');
            this.releaseSnapshot();
            this.objectURL = URL.createObjectURL(image);
            const link = document.createElement('a');
            link.href = this.objectURL;
            link.download = this.snapshotFilename(new Date());
            document.body.appendChild(link);
            link.click();
            link.remove();
            this.notice.textContent = 'Snapshot downloaded.';
            this.cleanupTimer = setTimeout(() => this.releaseSnapshot(), 1000);
        } catch (error) {
            if (!this.destroyed) this.notice.textContent = error.message;
        } finally {
            this.snapshotController = null;
            this.snapshotButton.disabled = false;
        }
    }

    updateFullscreenLabel() {
        const active = document.fullscreenElement === this.tile;
        this.fullscreenButton.textContent = active ? 'Exit fullscreen' : 'Fullscreen';
        this.fullscreenButton.setAttribute('aria-label', `${active ? 'Exit fullscreen' : 'Fullscreen'}: ${this.camera.name}`);
    }

    async fullscreen() {
        this.notice.textContent = '';
        try {
            if (document.fullscreenElement === this.tile) await document.exitFullscreen();
            else if (this.tile.requestFullscreen) await this.tile.requestFullscreen();
            else this.notice.textContent = 'Fullscreen is unavailable in this browser.';
        } catch {
            this.notice.textContent = 'Fullscreen could not open. Try again from the camera tab.';
        }
    }

    destroy() {
        this.destroyed = true;
        this.visible = false;
        this.disconnectPlayer();
        if (this.closingSocket && this.closeListener) this.closingSocket.removeEventListener('close', this.closeListener);
        this.closingSocket = null;
        this.closeListener = null;
        this.snapshotController?.abort();
        this.releaseSnapshot();
        this.card.remove();
    }
}

class SetupRow {
    constructor(camera) {
        this.node = find('#setup-camera-template').content.firstElementChild.cloneNode(true);
        this.renameForm = this.node.querySelector('.rename-form');
        this.input = this.node.querySelector('.rename-input');
        this.confirmation = this.node.querySelector('.remove-confirmation');
        this.actions = this.node.querySelector('.row-actions');
        this.error = this.node.querySelector('.row-error');
        this.node.querySelector('.edit-camera').addEventListener('click', () => this.edit());
        this.node.querySelector('.cancel-name').addEventListener('click', () => this.cancel());
        this.node.querySelector('.remove-camera').addEventListener('click', () => this.confirmRemove());
        this.node.querySelector('.cancel-remove').addEventListener('click', () => this.cancel());
        this.node.querySelector('.confirm-remove').addEventListener('click', () => this.remove());
        this.renameForm.addEventListener('submit', event => this.rename(event));
        this.update(camera);
    }

    update(camera) {
        this.camera = camera;
        this.node.querySelector('.row-name').textContent = camera.name;
        this.node.querySelector('.row-mac').textContent = camera.mac;
        this.node.querySelector('.row-ip').textContent = camera.ip || 'not found on the network';
        this.node.querySelector('.remove-question').textContent = `Remove ${camera.name}?`;
        this.confirmation.setAttribute('aria-label', `Remove ${camera.name}?`);
        this.input.setAttribute('aria-label', `New name for ${camera.name}`);
        this.node.querySelector('.edit-camera').setAttribute('aria-label', `Edit ${camera.name}`);
        this.node.querySelector('.remove-camera').setAttribute('aria-label', `Remove ${camera.name}`);
        this.updateStatus();
    }

    updateStatus() {
        const status = tiles.get(this.camera.id)?.state || 'Unavailable';
        const dot = this.node.querySelector('.camera-dot');
        dot.dataset.state = status.toLowerCase();
        dot.title = status;
    }

    edit() {
        this.error.textContent = '';
        this.actions.hidden = true;
        this.confirmation.hidden = true;
        this.renameForm.hidden = false;
        this.input.value = this.camera.name;
        this.input.focus();
        this.input.select();
    }

    cancel() {
        const wasRename = !this.renameForm.hidden;
        this.renameForm.hidden = true;
        this.confirmation.hidden = true;
        this.actions.hidden = false;
        this.error.textContent = '';
        this.node.querySelector(wasRename ? '.edit-camera' : '.remove-camera').focus();
    }

    confirmRemove() {
        this.error.textContent = '';
        this.renameForm.hidden = true;
        this.actions.hidden = true;
        this.confirmation.hidden = false;
        this.node.querySelector('.cancel-remove').focus();
    }

    async rename(event) {
        event.preventDefault();
        if (state.busy) return;
        const name = this.input.value.trim();
        if (!name || [...name].length > 40) {
            this.error.textContent = 'Enter a name with 1–40 characters.';
            this.input.focus();
            return;
        }
        setBusy(true);
        let saved = false;
        this.error.textContent = '';
        try {
            const camera = await request(`api/cameras/${encodeURIComponent(this.camera.id)}`, {method: 'PATCH', body: {name}});
            state.cameraRevision++;
            reconcile(state.cameras.map(item => item.id === camera.id ? camera : item));
            this.cancel();
            saved = true;
            announce(`Renamed to ${camera.name}.`);
        } catch (error) {
            this.error.textContent = error.message;
            scheduleLoad(5000);
        } finally {
            setBusy(false);
            if (setupDialog.open) {
                (saved ? this.node.querySelector('.edit-camera') : this.input).focus();
            }
        }
    }

    async remove() {
        if (state.busy) return;
        const {id, name} = this.camera;
        setBusy(true);
        state.videoChangePending = true;
        this.error.textContent = '';
        try {
            await request(`api/cameras/${encodeURIComponent(id)}`, {method: 'DELETE'});
            state.cameraRevision++;
            beginRestart();
            reconcile(state.cameras.filter(camera => camera.id !== id));
            announce(`Removed ${name}.`);
            await refreshAfterMutation();
            if (setupDialog.open) find('#close-setup').focus();
        } catch (error) {
            this.error.textContent = error.message;
            if (error.code === 'go2rtc') beginRestart();
            scheduleLoad(5000);
        } finally {
            state.videoChangePending = false;
            setBusy(false);
        }
    }
}

function placeNode(parent, node, index) {
    if (parent.children[index] !== node) parent.insertBefore(node, parent.children[index] || null);
}

function reconcile(cameras) {
    const ids = new Set(cameras.map(camera => camera.id));
    for (const [id, tile] of tiles) {
        if (!ids.has(id)) {
            tile.destroy();
            tiles.delete(id);
            rows.get(id).node.remove();
            rows.delete(id);
        }
    }
    state.cameras = cameras;
    cameras.forEach((camera, index) => {
        if (tiles.has(camera.id)) tiles.get(camera.id).update(camera);
        else tiles.set(camera.id, new CameraTile(camera));
        if (rows.has(camera.id)) rows.get(camera.id).update(camera);
        else rows.set(camera.id, new SetupRow(camera));
        placeNode(find('#setup-camera-list'), rows.get(camera.id).node, index);
    });
    renderPage();
    updateSetup();
}

function effectiveLayout() {
    return mobile.matches ? 'auto' : state.layout;
}

function renderPage() {
    const layout = effectiveLayout();
    const slots = layout === 'auto' ? Math.max(1, state.cameras.length)
        : Number(layout.split('x')[0]) * Number(layout.split('x')[1]);
    state.pageCount = Math.max(1, Math.ceil(state.cameras.length / slots));
    state.page = Math.max(0, Math.min(state.page, state.pageCount - 1));
    const visible = state.cameras.slice(state.page * slots, (state.page + 1) * slots);
    state.visible = visible.map(camera => camera.id);
    const visibleIds = new Set(state.visible);
    for (const [id, tile] of tiles) if (!visibleIds.has(id)) tile.setVisible(false);
    for (const placeholder of grid.querySelectorAll('.empty-slot')) placeholder.remove();
    visible.forEach((camera, index) => {
        const tile = tiles.get(camera.id);
        placeNode(grid, tile.card, index);
        tile.setVisible(true);
    });
    if (layout !== 'auto' && state.cameras.length) {
        for (let index = visible.length; index < slots; index++) {
            const placeholder = document.createElement('div');
            placeholder.className = 'empty-slot';
            placeholder.textContent = 'Empty';
            grid.appendChild(placeholder);
        }
    }
    grid.dataset.layout = layout;
    grid.dataset.auto = state.cameras.length === 1 ? 'single' : state.cameras.length === 2 ? 'pair'
        : state.cameras.length <= 4 ? 'quad' : 'many';
    dashboard.dataset.effectiveLayout = layout;
    dashboard.classList.toggle('many-cameras', layout === 'auto' && state.cameras.length > 4);
    grid.hidden = !state.cameras.length;
    find('#empty-state').hidden = Boolean(state.cameras.length);
    if (state.loaded && !state.cameras.length) {
        find('#empty-title').textContent = 'No cameras yet';
        find('#empty-description').textContent = 'Open Setup to add your first camera by its MAC address.';
    }
    find('#pagination').hidden = state.pageCount <= 1;
    find('#page-label').textContent = `Page ${state.page + 1} of ${state.pageCount}`;
    find('#previous-page').disabled = state.page === 0;
    find('#next-page').disabled = state.page >= state.pageCount - 1;
    layoutButtons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.layout === state.layout)));
    updateSummary();
}

function updateSummary() {
    const live = state.visible.filter(id => tiles.get(id)?.state === 'Live').length;
    const total = state.cameras.length;
    const text = `${live} of ${state.visible.length} live · ${total} ${total === 1 ? 'camera' : 'cameras'}`;
    if (summary.textContent !== text) summary.textContent = text;
    rows.forEach(row => row.updateStatus());
    if (state.restartUntil && !state.videoChangePending && state.visible.length &&
        state.visible.every(id => tiles.get(id).state === 'Live')) endRestart();
}

function beginRestart() {
    clearTimeout(restartTimer);
    state.restartUntil = performance.now() + 10000;
    find('#restart-banner').hidden = false;
    for (const tile of tiles.values()) if (tile.visible) tile.resetTracking();
    restartTimer = setTimeout(endRestart, 10000);
}

function endRestart() {
    clearTimeout(restartTimer);
    state.restartUntil = 0;
    find('#restart-banner').hidden = true;
}

function updateSetup() {
    const atLimit = state.cameras.length >= state.max;
    find('#camera-count').textContent = `${state.cameras.length} of ${state.max} cameras`;
    find('#add-limit').textContent = !state.loaded ? 'Waiting for the camera list…'
        : atLimit ? 'Camera limit reached. Remove a camera before adding another.' : '';
    find('#add-fields').disabled = state.busy || atLimit || !state.loaded;
    find('#manage-fields').disabled = state.busy;
    find('#setup-empty').hidden = Boolean(state.cameras.length);
}

function setBusy(busy) {
    state.busy = busy;
    updateSetup();
}

function announce(message) {
    find('#setup-announcement').textContent = message;
}

function scheduleLoad(delay) {
    clearTimeout(loadTimer);
    loadTimer = setTimeout(loadCameras, delay);
}

function validMetadata(data) {
    return data && layouts.includes(data.layout) && Number.isInteger(data.max) && data.max > 0 &&
        Array.isArray(data.cameras) && data.cameras.every(camera => camera &&
            typeof camera.id === 'string' && /^cam[1-9][0-9]{0,3}$/.test(camera.id) &&
            typeof camera.name === 'string' && typeof camera.mac === 'string' &&
            (camera.ip === null || typeof camera.ip === 'string') && typeof camera.available === 'boolean') &&
        new Set(data.cameras.map(camera => camera.id)).size === data.cameras.length;
}

function loadCameras() {
    if (loadPromise) return loadPromise;
    clearTimeout(loadTimer);
    const cameraRevision = state.cameraRevision;
    const layoutRevision = state.layoutRevision;
    loadPromise = (async () => {
        try {
            const data = await request('api/cameras');
            if (!validMetadata(data)) throw new ApiError('Camera list is unavailable. Retrying…');
            if (cameraRevision !== state.cameraRevision) {
                scheduleLoad(0);
                return false;
            }
            state.loaded = true;
            state.max = data.max;
            if (layoutRevision === state.layoutRevision && !state.layoutDirty && !layoutSaving) state.layout = data.layout;
            reconcile(data.cameras);
            find('#connection-message').hidden = true;
            find('#retry-cameras').hidden = true;
            scheduleLoad(30000);
            return true;
        } catch (error) {
            find('#connection-message').textContent = error.message;
            find('#connection-message').hidden = false;
            find('#retry-cameras').hidden = false;
            if (!state.cameras.length) {
                find('#empty-title').textContent = 'Cameras unavailable';
                find('#empty-description').textContent = error.message;
            }
            scheduleLoad(5000);
            return false;
        } finally {
            loadPromise = null;
        }
    })();
    return loadPromise;
}

async function refreshAfterMutation() {
    if (loadPromise) await loadPromise;
    await loadCameras();
}

async function addCamera(event) {
    event.preventDefault();
    if (state.busy || !state.loaded || state.cameras.length >= state.max) return;
    const mac = normalizeMac(macInput.value);
    find('#mac-error').textContent = mac ? '' : 'Enter a valid MAC address with six hexadecimal octets.';
    macInput.setAttribute('aria-invalid', String(!mac));
    if (!mac) { macInput.focus(); return; }
    const body = {mac};
    if (nameInput.value.trim()) body.name = nameInput.value.trim();
    setBusy(true);
    state.videoChangePending = true;
    find('#add-error').textContent = '';
    find('#add-progress').textContent = 'Looking for the camera on your network…';
    announce('');
    try {
        const camera = await request('api/cameras', {method: 'POST', body});
        state.cameraRevision++;
        beginRestart();
        reconcile([...state.cameras, camera]);
        addForm.reset();
        macInput.removeAttribute('aria-invalid');
        announce(`Added ${camera.name}.`);
        await refreshAfterMutation();
    } catch (error) {
        find('#add-error').textContent = error.message;
        if (error.code === 'go2rtc') beginRestart();
        scheduleLoad(5000);
    } finally {
        state.videoChangePending = false;
        find('#add-progress').textContent = '';
        setBusy(false);
        if (setupDialog.open && [document.body, setupDialog, macInput, nameInput].includes(document.activeElement)) {
            (find('#add-fields').disabled ? find('#close-setup') : macInput).focus();
        }
    }
}

function selectLayout(layout) {
    state.layout = layout;
    state.page = 0;
    state.layoutRevision++;
    state.layoutDirty = true;
    queuedLayout = layout;
    renderPage();
    saveLayouts();
}

async function saveLayouts() {
    if (layoutSaving) return;
    layoutSaving = true;
    while (queuedLayout !== null) {
        const layout = queuedLayout;
        queuedLayout = null;
        find('#layout-message').textContent = 'Saving layout…';
        find('#retry-layout').hidden = true;
        try {
            await request('api/layout', {method: 'PUT', body: {layout}});
            if (layout === state.layout && queuedLayout === null) {
                state.layoutDirty = false;
                find('#layout-message').textContent = '';
            }
        } catch (error) {
            if (layout === state.layout && queuedLayout === null) {
                find('#layout-message').textContent = `Layout not saved. ${error.message}`;
                find('#retry-layout').hidden = false;
            }
        }
    }
    layoutSaving = false;
}

function openSetup() {
    if (setupDialog.open) return;
    setupDialog.showModal();
    (find('#add-fields').disabled ? find('#close-setup') : macInput).focus();
}

function changePage(delta) {
    state.page = Math.max(0, Math.min(state.page + delta, state.pageCount - 1));
    renderPage();
}

setupButton.addEventListener('click', openSetup);
find('#empty-setup').addEventListener('click', openSetup);
find('#close-setup').addEventListener('click', () => setupDialog.close());
setupDialog.addEventListener('close', () => setupButton.focus());
addForm.addEventListener('submit', addCamera);
macInput.addEventListener('input', () => {
    find('#mac-error').textContent = '';
    macInput.removeAttribute('aria-invalid');
});
find('#previous-page').addEventListener('click', () => changePage(-1));
find('#next-page').addEventListener('click', () => changePage(1));
find('#retry-cameras').addEventListener('click', loadCameras);
find('#retry-layout').addEventListener('click', () => {
    state.layoutRevision++;
    queuedLayout = state.layout;
    saveLayouts();
});
layoutButtons.forEach(button => button.addEventListener('click', () => selectLayout(button.dataset.layout)));
mobile.addEventListener('change', renderPage);
document.addEventListener('keydown', event => {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || setupDialog.open ||
        event.target.closest('input, textarea, select, [contenteditable]')) return;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        if (state.pageCount <= 1) return;
        event.preventDefault();
        changePage(event.key === 'ArrowLeft' ? -1 : 1);
    }
});
document.addEventListener('visibilitychange', () => {
    tiles.forEach(tile => tile.visibilityChanged());
    updateSummary();
});
document.addEventListener('fullscreenchange', () => tiles.forEach(tile => tile.updateFullscreenLabel()));

function updateClock() {
    const now = new Date();
    clock.dateTime = now.toISOString();
    clock.textContent = now.toLocaleTimeString([], {hour12: false});
}

// Unattended wall displays: reload once a day, between 04:00 and 05:00 local time.
const RELOAD_HOUR = 4;
const pageLoadedAt = Date.now();
let reloadPending = false;

function dailyReload() {
    if (reloadPending || new Date().getHours() !== RELOAD_HOUR || Date.now() - pageLoadedAt < 3600000) return;
    if (setupDialog.open || document.fullscreenElement) return;
    reloadPending = true;
    // Reload only while the server answers, so the display never lands on a browser error page.
    fetch('api/cameras', {cache: 'no-store'})
        .then(response => {
            if (!response.ok) throw new Error('not ready');
            location.reload();
        })
        .catch(() => setTimeout(() => { reloadPending = false; }, 60000));
}

updateClock();
loadCameras();
setInterval(() => {
    updateClock();
    dailyReload();
    tiles.forEach(tile => tile.updatePlaybackStatus());
    updateSummary();
}, 1000);
