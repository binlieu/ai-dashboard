const themeValues = ['default', 'mission', 'hud', 'observatory'];
const initialTheme = readTheme();
applyTheme(initialTheme);

import './vendor/video-stream.js';

function readTheme() {
    try {
        const value = localStorage.getItem('kcd.theme');
        return themeValues.includes(value) ? value : 'default';
    } catch { return 'default'; }
}

function applyTheme(value) {
    if (value === 'default') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = value;
}

const find = selector => document.querySelector(selector);
const dashboard = find('.dashboard');
const grid = find('#camera-grid');
const clock = find('#clock');
const date = find('#date');
const summary = find('#summary');
const statusBar = find('.status-bar');
const setupDialog = find('#setup-dialog');
const setupButton = find('#setup-button');
const addForm = find('#add-camera-form');
const macInput = find('#camera-mac');
const nameInput = find('#camera-name');
const rotationSelect = find('#rotation-interval');
const themeSelect = find('#theme-select');
const infoTile = find('#info-tile');
const weatherForm = find('#weather-form');
const deviceControls = find('#device-controls');
const deviceList = find('#setup-device-list');
const deviceForm = find('#add-device-form');
const deviceMac = find('#device-mac');
const deviceName = find('#device-name');
const backupSection = find('#setup-backup');
const backupForm = find('#backup-form');
const backupFields = find('#backup-fields');
const backupFolder = find('#backup-folder');
const backupList = find('#backup-camera-list');
const backupSave = find('#backup-save');
const backupError = find('#backup-error');
const backupConfirmation = find('#backup-confirmation');
const rotationValues = ['0', '10', '30', '60', '300'];
const layoutButtons = [...document.querySelectorAll('.layout-options button')];
const mobile = matchMedia('(max-width: 699px)');
const portraitDisplay = matchMedia('(orientation: portrait) and (min-width: 700px)');
dashboard.dataset.orientation = portraitDisplay.matches ? 'portrait' : 'landscape';
const layouts = ['auto', '2x1', '2x2', '3x3', '4x4', 'spotlight'];
const tiles = new Map();
let fullscreenTile = null;
const rows = new Map();
const deviceViews = new Map();
const deviceStates = {on: 'On', off: 'Off', unknown: 'Unknown'};
let devices = [];
let devicesSupported = null;
let deviceMax = 6;
let deviceSetupBusy = false;
let deviceSetupRevision = 0;
let deviceRead = null;
let nextDeviceRead = 0;
let deviceNoticeUntil = 0;
const backupRows = new Map();
const backupStates = {
    recording: 'Recording', waiting: 'Waiting for video', no_folder: 'No folder',
    low_space: 'Drive almost full', save_failed: 'Not saving files', off: 'Off',
};
const backupWarnings = {folder_missing: 'Backup: drive missing', low_space: 'Backup: drive almost full', save_failed: 'Backup: files not saving'};
const backupSizeFormat = new Intl.NumberFormat(undefined, {maximumSignificantDigits: 2});
const backupTimeFormat = new Intl.DateTimeFormat(undefined, {hour: '2-digit', minute: '2-digit', hourCycle: 'h23'});
let backupData = null;
let backupSaving = false;
let pendingBackup = null;
let backupRead = null;
let lastBackupReadAt = -Infinity;
const state = {
    cameras: [], max: 16, loaded: false, layout: 'auto', page: 0, pageCount: 1,
    visible: [], busy: false, videoChangePending: false, restartUntil: 0,
    cameraRevision: 0, layoutRevision: 0, layoutDirty: false,
    spotlight: readSpotlight(), loadFailed: false,
    rotationSeconds: readRotationInterval(),
    weatherEnabled: false,
};
let loadTimer = 0;
let loadPromise = null;
let restartTimer = 0;
let layoutSaving = false;
let queuedLayout = null;
let activeUntil = performance.now() + 4000;
let idleTimer = 0;
let pointerOverBar = false;
let lastPointer = null;
let nextRotationAt = Date.now() + state.rotationSeconds * 1000;
let weatherData = null;
let weatherPromise = null;
let weatherSaving = false;
let weatherRevision = 0;
let weatherFormRevision = 0;
let nextWeatherAt = 0;
let weatherPollUntil = 0;
let infoMinute = '';
let infoDay = '';
const timezoneFormatters = new Map();
let displayFormatters = null;

function readSpotlight() {
    try { return localStorage.getItem('kcd.spotlight'); } catch { return null; }
}

function readRotationInterval() {
    try {
        const value = localStorage.getItem('kcd.rotate');
        return rotationValues.includes(value) ? Number(value) : 0;
    } catch { return 0; }
}

function resetRotation() {
    nextRotationAt = Date.now() + state.rotationSeconds * 1000;
}

function changeRotationInterval() {
    state.rotationSeconds = rotationValues.includes(rotationSelect.value) ? Number(rotationSelect.value) : 0;
    rotationSelect.value = String(state.rotationSeconds);
    try { localStorage.setItem('kcd.rotate', rotationSelect.value); } catch { /* Storage can be disabled. */ }
    resetRotation();
    announce(state.rotationSeconds ? `Rotation set to ${rotationSelect.selectedOptions[0].textContent}.` : 'Rotation off.');
}

function rotateSpotlight() {
    if (!state.rotationSeconds || effectiveLayout() !== 'spotlight' || document.hidden ||
        setupDialog.open || document.fullscreenElement || Date.now() < nextRotationAt) return;
    const currentIndex = state.cameras.findIndex(camera => camera.id === state.spotlight);
    for (let offset = 1; offset < state.cameras.length; offset++) {
        const camera = state.cameras[(currentIndex + offset) % state.cameras.length];
        if (state.visible.includes(camera.id) && tiles.get(camera.id)?.state === 'Live') {
            selectSpotlight(camera.id, {automatic: true});
            return;
        }
    }
    resetRotation();
}

function updateActivity() {
    clearTimeout(idleTimer);
    const remaining = activeUntil - performance.now();
    const heldActive = !state.cameras.length || state.loadFailed || setupDialog.open ||
        pointerOverBar || statusBar.contains(document.activeElement);
    dashboard.dataset.idle = String(!heldActive && remaining <= 0);
    if (remaining > 0) idleTimer = setTimeout(updateActivity, remaining);
}

function activate() {
    activeUntil = performance.now() + 4000;
    updateActivity();
}

function selectSpotlight(id, {automatic = false} = {}) {
    state.spotlight = id;
    try { localStorage.setItem('kcd.spotlight', id); } catch { /* Storage can be disabled. */ }
    resetRotation();
    if (!automatic) activate();
    renderPage();
    if (!automatic) tiles.get(id).fullscreenButton.focus();
}

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

function conditionFor(code) {
    if (code === 0) return 'Clear';
    if (code === 1 || code === 2) return 'Partly cloudy';
    if (code === 45 || code === 48) return 'Fog';
    if (code >= 51 && code <= 57) return 'Drizzle';
    if (code >= 61 && code <= 67) return 'Rain';
    if ((code >= 71 && code <= 77) || code === 85 || code === 86) return 'Snow';
    if (code >= 80 && code <= 82) return 'Showers';
    if (code >= 95 && code <= 99) return 'Thunderstorm';
    return 'Cloudy';
}

function weatherIcon(code, isDay = true) {
    const condition = conditionFor(code);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.classList.add('weather-icon');
    svg.setAttribute('viewBox', '0 0 32 32');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', condition);
    svg.setAttribute('focusable', 'false');
    const paths = [];
    const sun = 'M16 5a6 6 0 1 0 0 12 6 6 0 0 0 0-12 M16 1v2 M16 19v2 M6 11H4 M28 11h-2 M9 4 7 2 M23 4l2-2 M9 18l-2 2 M23 18l2 2';
    const moon = 'M19 2a9 9 0 1 0 9 13A10 10 0 0 1 19 2Z';
    const cloud = 'M8 22a5 5 0 1 1 1-10 7 7 0 0 1 13 1 4.5 4.5 0 1 1 2 9Z';
    if (condition === 'Clear' || condition === 'Partly cloudy') paths.push(isDay ? sun : moon);
    if (condition !== 'Clear') paths.push(cloud);
    if (condition === 'Fog') paths.push('M5 26h22 M9 30h14');
    if (condition === 'Drizzle') paths.push('M10 26h.1 M16 28h.1 M22 26h.1');
    if (condition === 'Rain') paths.push('M10 25l-2 4 M17 25l-2 4 M24 25l-2 4');
    if (condition === 'Showers') paths.push('M10 25l-2 4 M22 25l-2 4 M16 26h.1');
    if (condition === 'Snow') paths.push('M10 25v6 M7 28h6 M22 25v6 M19 28h6');
    if (condition === 'Thunderstorm') paths.push('M17 22l-4 5h5l-3 4');
    paths.forEach(path => {
        const shape = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        shape.setAttribute('d', path);
        svg.appendChild(shape);
    });
    return svg;
}

function weatherTimeFormatters(data) {
    const timeZone = data.enabled && typeof data.timezone === 'string' ? data.timezone : null;
    if (timeZone === null) return null;
    if (!timezoneFormatters.has(timeZone)) {
        try {
            timezoneFormatters.set(timeZone, {
                parts: new Intl.DateTimeFormat('en-US', {timeZone, calendar: 'gregory', numberingSystem: 'latn',
                    year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', hourCycle: 'h23'}),
                statusTime: new Intl.DateTimeFormat([], {timeZone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}),
                statusDate: new Intl.DateTimeFormat([], {timeZone, weekday: 'short', day: 'numeric', month: 'short'}),
                infoTime: new Intl.DateTimeFormat([], {timeZone, hour: 'numeric', minute: '2-digit'}),
                infoDate: new Intl.DateTimeFormat([], {timeZone, weekday: 'long', month: 'long', day: 'numeric'}),
            });
        } catch {
            timezoneFormatters.set(timeZone, null);
        }
    }
    return timezoneFormatters.get(timeZone);
}

function displayTimeParts(now) {
    if (!displayFormatters) {
        return {year: now.getFullYear(), month: now.getMonth() + 1, day: now.getDate(),
            hour: now.getHours(), minute: now.getMinutes()};
    }
    const parts = {};
    for (const part of displayFormatters.parts.formatToParts(now)) {
        if (['year', 'month', 'day', 'hour', 'minute'].includes(part.type)) parts[part.type] = Number(part.value);
    }
    return parts;
}

function displayDate(parts) {
    return `${parts.year}-${String(parts.month).padStart(2, '0')}-${String(parts.day).padStart(2, '0')}`;
}

function formatInfoTime(now) {
    return displayFormatters ? displayFormatters.infoTime.format(now)
        : now.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
}

function freshWeatherForecast(now = Date.now()) {
    const forecast = weatherData?.forecast;
    return weatherData?.status === 'ok' && forecast && now / 1000 - forecast.updated <= 3 * 3600
        ? forecast : null;
}

function buildForecastDays(days, today) {
    return days.filter(day => day.date >= today).slice(0, 5).map(day => {
        const node = document.createElement('div');
        node.className = 'forecast-day';
        const label = document.createElement('span');
        label.textContent = day.date === today ? 'Today'
            : new Date(`${day.date}T12:00:00`).toLocaleDateString([], {weekday: 'short'});
        const range = document.createElement('span');
        range.textContent = `${day.high}° / ${day.low}°`;
        const rain = document.createElement('span');
        rain.className = 'forecast-rain';
        rain.textContent = day.rain >= 20 ? `${day.rain}%` : '';
        node.append(label, weatherIcon(day.code), range, rain);
        return node;
    });
}

function renderWeather() {
    const forecast = freshWeatherForecast();
    const today = forecast && displayDate(displayTimeParts(new Date()));
    fullscreenTile?.renderFullscreenForecast(forecast, today);
    find('#info-message').hidden = Boolean(forecast);
    find('#info-weather').hidden = !forecast;
    find('#info-message').textContent = weatherData?.status === 'loading' && !weatherData.forecast
        ? 'Loading weather…' : 'Weather unavailable';
    if (!forecast) return;
    const current = forecast.current;
    find('#info-temperature').textContent = `${current.temp}°`;
    find('#info-condition').replaceChildren(weatherIcon(current.code, current.is_day), document.createTextNode(conditionFor(current.code)));
    find('#info-details').textContent = `Feels ${current.feels}° · Wind ${current.wind} ${weatherData.units === 'c' ? 'km/h' : 'mph'}`;
    find('#info-place').textContent = weatherData.name;
    find('#info-place').title = weatherData.name;
    find('#info-forecast').replaceChildren(...buildForecastDays(forecast.days, today));
}

function acceptWeather(data) {
    if (!data || typeof data.enabled !== 'boolean' || !['f', 'c'].includes(data.units) ||
        typeof data.place !== 'string' || typeof data.name !== 'string' ||
        !['off', 'loading', 'ok', 'unavailable'].includes(data.status)) throw new ApiError('Weather is unavailable.');
    const previousFormatters = displayFormatters;
    weatherData = data;
    displayFormatters = weatherTimeFormatters(data);
    const changed = state.weatherEnabled !== data.enabled;
    state.weatherEnabled = data.enabled;
    if (changed) renderPage();
    renderWeather();
    const now = Date.now();
    if (data.status === 'loading') {
        if (!weatherPollUntil) weatherPollUntil = now + 60000;
        nextWeatherAt = now < weatherPollUntil ? Math.min(now + 5000, weatherPollUntil) : now + 300000;
    } else {
        weatherPollUntil = 0;
        nextWeatherAt = now + 300000;
    }
    if (displayFormatters !== previousFormatters) {
        infoMinute = '';
        infoDay = '';
        updateClock();
        weatherTick();
    } else {
        fullscreenTile?.renderFullscreenInfo(new Date(now));
    }
}

function loadWeather() {
    if (weatherSaving) return Promise.resolve(null);
    if (weatherPromise) return weatherPromise;
    const revision = weatherRevision;
    weatherPromise = (async () => {
        try {
            const data = await request('api/weather');
            if (revision !== weatherRevision) return null;
            acceptWeather(data);
            return data;
        } catch {
            if (revision === weatherRevision) {
                nextWeatherAt = Date.now() + 300000;
                weatherPollUntil = 0;
                if (!weatherData?.forecast) {
                    find('#info-message').textContent = 'Weather unavailable';
                    find('#info-weather').hidden = true;
                    find('#info-message').hidden = false;
                }
            }
            return null;
        } finally { weatherPromise = null; }
    })();
    return weatherPromise;
}

function fillWeatherSettings(data) {
    find('#weather-enabled').checked = data.enabled;
    find('#weather-place').value = data.place;
    find('#weather-units').value = data.units;
    find('#weather-fields').disabled = weatherSaving;
    find('#weather-result').textContent = data.enabled && data.name ? `Showing weather for ${data.name}.` : '';
}

async function loadWeatherSettings() {
    const revision = ++weatherFormRevision;
    if (weatherData && !weatherSaving) fillWeatherSettings(weatherData);
    const data = await loadWeather();
    if (!setupDialog.open || weatherSaving || revision !== weatherFormRevision) return;
    if (data) fillWeatherSettings(data);
    else {
        find('#weather-fields').disabled = false;
        find('#weather-error').textContent = 'Weather settings could not load. Try saving again.';
    }
}

async function saveWeather(event) {
    event.preventDefault();
    if (weatherSaving) return;
    const body = {enabled: find('#weather-enabled').checked, place: find('#weather-place').value, units: find('#weather-units').value};
    weatherSaving = true;
    weatherRevision++;
    weatherFormRevision++;
    find('#weather-fields').disabled = true;
    find('#weather-progress').textContent = 'Looking up the place…';
    find('#weather-error').textContent = '';
    find('#weather-result').textContent = '';
    try {
        const data = await request('api/weather', {method: 'PUT', body});
        weatherPollUntil = 0;
        acceptWeather(data);
        fillWeatherSettings(data);
        find('#weather-result').textContent = data.enabled ? `Showing weather for ${data.name}.` : 'Info tile hidden.';
        // Let an older GET finish before fetching the newly saved settings.
        if (weatherPromise) await weatherPromise;
        weatherSaving = false;
        await loadWeather();
    } catch (error) {
        find('#weather-error').textContent = error.message;
    } finally {
        weatherSaving = false;
        find('#weather-fields').disabled = false;
        find('#weather-progress').textContent = '';
    }
}

function weatherTick() {
    const now = new Date();
    const parts = displayTimeParts(now);
    const day = displayDate(parts);
    const minute = `${day}:${parts.hour}:${parts.minute}`;
    if (minute !== infoMinute) {
        infoMinute = minute;
        find('#info-clock').dateTime = now.toISOString();
        find('#info-clock').textContent = formatInfoTime(now);
        fullscreenTile?.renderFullscreenInfo(now);
    }
    if (day !== infoDay) {
        infoDay = day;
        find('#info-date').dateTime = day;
        find('#info-date').textContent = displayFormatters ? displayFormatters.infoDate.format(now)
            : now.toLocaleDateString([], {weekday: 'long', month: 'long', day: 'numeric'});
        renderWeather();
    }
    if (weatherData?.forecast && now.getTime() / 1000 - weatherData.forecast.updated > 3 * 3600 && !find('#info-weather').hidden) renderWeather();
    if (now.getTime() >= nextWeatherAt) loadWeather();
}

function normalizeMac(value) {
    let text = value.trim();
    if (/^[0-9a-f]{12}$/i.test(text)) text = text.match(/.{2}/g).join(':');
    if (!/^([0-9a-f]{1,2}:){5}[0-9a-f]{1,2}$/i.test(text) &&
        !/^([0-9a-f]{1,2}-){5}[0-9a-f]{1,2}$/i.test(text)) return null;
    return text.replace(/-/g, ':').split(':').map(octet => octet.toLowerCase().padStart(2, '0')).join(':');
}

function addIcon(button, path) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    const shape = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    shape.setAttribute('d', path);
    svg.appendChild(shape);
    button.appendChild(svg);
}

class CameraTile {
    constructor(camera) {
        this.camera = camera;
        this.state = 'Paused';
        this.offlineSince = null;
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
        this.card.hidden = true;
        this.tile = this.card;
        this.label = this.card.querySelector('.tile-label-text');
        this.dot = this.card.querySelector('.camera-dot');
        this.controls = this.card.querySelector('.controls');
        this.spotlightButton = this.card.querySelector('.spotlight-select');
        this.snapshotButton = this.card.querySelector('.snapshot');
        this.fullscreenButton = this.card.querySelector('.fullscreen');
        this.notice = this.card.querySelector('.notice');
        const fullscreenInfo = document.createElement('div');
        fullscreenInfo.className = 'fullscreen-info';
        fullscreenInfo.setAttribute('aria-hidden', 'true');
        this.fullscreenClock = document.createElement('div');
        this.fullscreenClock.className = 'fullscreen-info-clock';
        this.fullscreenWeather = document.createElement('div');
        this.fullscreenWeather.className = 'fullscreen-info-weather';
        this.fullscreenWeather.hidden = true;
        this.fullscreenForecast = null;
        fullscreenInfo.append(this.fullscreenClock, this.fullscreenWeather);
        this.card.appendChild(fullscreenInfo);
        this.fullscreenForecastBand = document.createElement('div');
        this.fullscreenForecastBand.className = 'fullscreen-forecast';
        this.fullscreenForecastBand.setAttribute('aria-hidden', 'true');
        this.fullscreenForecastBand.hidden = true;
        this.card.appendChild(this.fullscreenForecastBand);
        addIcon(this.snapshotButton, 'M3 7h4l2-3h6l2 3h4v13H3Z M16 13a4 4 0 1 1-8 0 4 4 0 0 1 8 0');
        addIcon(this.fullscreenButton, 'M3 9V3h6 M15 3h6v6 M21 15v6h-6 M9 21H3v-6');
        this.snapshotButton.addEventListener('click', () => this.snapshot());
        this.fullscreenButton.addEventListener('click', () => this.fullscreen());
        this.spotlightButton.addEventListener('click', () => selectSpotlight(this.camera.id));
        this.card.addEventListener('dblclick', event => {
            if (!this.card.classList.contains('spotlight-strip') && !event.target.closest('button')) this.fullscreen();
        });
        this.update(camera);
    }

    update(camera) {
        const wasAvailable = this.camera.available;
        this.camera = camera;
        this.tile.setAttribute('aria-label', `${camera.name} video`);
        this.snapshotButton.setAttribute('aria-label', `Snapshot of ${camera.name}`);
        this.spotlightButton.setAttribute('aria-label', `Show ${camera.name} large`);
        this.updateFullscreenLabel();
        if (!camera.available) {
            this.disconnectPlayer();
            this.showStatus('Unavailable');
        } else if (!wasAvailable && this.visible) {
            this.resetTracking();
        }
        this.updateLabel();
    }

    showStatus(label) {
        if (label === 'Live') this.offlineSince = null;
        else if (this.state === 'Live') this.offlineSince = performance.now();
        this.state = label;
        this.updateLabel();
    }

    updateLabel() {
        const status = this.state.toLowerCase();
        let suffix = status === 'live' ? '' : ` · ${status}`;
        if (status === 'unavailable') suffix = ' · not found';
        if (status === 'offline') {
            const minutes = Math.floor((performance.now() - (this.offlineSince ?? performance.now())) / 60000);
            suffix = ` · offline ${minutes < 1 ? '<1' : minutes} min`;
        }
        const text = this.camera.name + suffix;
        if (this.label.textContent !== text) this.label.textContent = text;
        this.dot.dataset.state = status;
        this.card.dataset.state = status;
        this.card.classList.toggle('is-offline', status === 'offline' || status === 'unavailable');
    }

    setRole(spotlight, strip) {
        this.card.classList.toggle('spotlight-large', spotlight);
        this.card.classList.toggle('spotlight-strip', strip);
        this.controls.hidden = strip;
        this.spotlightButton.hidden = !strip;
    }

    resetTracking() {
        this.watchStartedAt = performance.now();
        this.lastProgressAt = null;
        this.previousTime = this.player?.video?.currentTime ?? 0;
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
        this.card.hidden = !visible;
        if (visible) {
            if (this.offlineSince === null) this.offlineSince = performance.now();
            this.resetTracking();
            this.connectPlayer();
        } else {
            this.disconnectPlayer();
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

    videoFrame() {
        const video = this.player?.video;
        if (!video || video.readyState < 2 || !video.videoWidth || !video.videoHeight) return Promise.resolve(null);
        try {
            const canvas = document.createElement('canvas');
            canvas.width = video.videoWidth;
            canvas.height = video.videoHeight;
            canvas.getContext('2d').drawImage(video, 0, 0);
            return new Promise(resolve => canvas.toBlob(blob => resolve(blob && blob.size ? blob : null), 'image/jpeg', 0.92));
        } catch {
            return Promise.resolve(null);
        }
    }

    async snapshot() {
        this.snapshotButton.disabled = true;
        this.notice.textContent = '';
        this.snapshotController = new AbortController();
        try {
            // Prefer the frame already playing in the tile: it needs no ffmpeg on the server.
            const image = await this.videoFrame() || await request(`api/frame.jpeg?src=${encodeURIComponent(this.camera.id)}`, {
                image: true, signal: this.snapshotController.signal,
            }).catch(error => {
                if (error.name === 'AbortError') throw error;
                throw new ApiError('Snapshot unavailable until the video is playing. Try again in a moment.');
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
        this.fullscreenButton.setAttribute('aria-label', `${active ? 'Exit fullscreen for' : 'Fullscreen for'} ${this.camera.name}`);
        this.fullscreenButton.setAttribute('aria-pressed', String(active));
    }

    renderFullscreenInfo(now) {
        this.fullscreenClock.textContent = formatInfoTime(now);
        const forecast = state.weatherEnabled ? freshWeatherForecast(now.getTime()) : null;
        const today = displayDate(displayTimeParts(now));
        if (forecast === this.fullscreenForecast && today === this.fullscreenDay) return;
        this.fullscreenForecast = forecast;
        this.fullscreenDay = today;
        // Today's forecast for the weather place, e.g. "Springfield · 42° / 26° Cloudy".
        const day = forecast?.days.find(entry => entry.date === today);
        this.fullscreenWeather.hidden = !day;
        if (!day) return;
        const city = weatherData.name.split(',')[0].trim();
        const conditions = document.createElement('span');
        conditions.textContent = `${city ? `${city} · ` : ''}${day.high}° / ${day.low}° ${conditionFor(day.code)}`;
        this.fullscreenWeather.replaceChildren(weatherIcon(day.code), conditions);
    }

    renderFullscreenForecast(forecast, today) {
        const days = state.weatherEnabled && forecast ? buildForecastDays(forecast.days, today) : [];
        this.fullscreenForecastBand.hidden = days.length === 0;
        this.fullscreenForecastBand.replaceChildren(...days);
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
            refreshBackup(true);
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
            refreshBackup(true);
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

function validDevice(device) {
    return device && typeof device.id === 'string' && /^dev[1-9][0-9]{0,3}$/.test(device.id) &&
        typeof device.name === 'string' && typeof device.mac === 'string' && validDeviceState(device.state);
}

function validDeviceState(value) {
    return value === 'on' || value === 'off' || value === 'unknown';
}

class DeviceView {
    constructor(device) {
        this.node = find('#setup-device-template').content.firstElementChild.cloneNode(true);
        this.renameForm = this.node.querySelector('.rename-form');
        this.input = this.node.querySelector('.rename-input');
        this.confirmation = this.node.querySelector('.remove-confirmation');
        this.actions = this.node.querySelector('.row-actions');
        this.error = this.node.querySelector('.row-error');
        this.renameButton = this.node.querySelector('.rename-device');
        this.removeButton = this.node.querySelector('.remove-device');
        this.busy = false;
        this.writing = false;

        this.renameButton.addEventListener('click', () => this.showAction('rename'));
        this.removeButton.addEventListener('click', () => this.showAction('remove'));
        this.node.querySelector('.cancel-name').addEventListener('click', () => this.cancel(this.renameButton));
        this.node.querySelector('.cancel-remove').addEventListener('click', () => this.cancel(this.removeButton));
        this.renameForm.addEventListener('submit', event => this.rename(event));
        this.node.querySelector('.confirm-remove').addEventListener('click', () => this.remove());

        this.button = document.createElement('button');
        this.button.type = 'button';
        this.button.className = 'device-button';
        this.buttonName = document.createElement('span');
        this.buttonName.className = 'device-button-name';
        this.buttonState = document.createElement('span');
        this.button.append(this.buttonName, this.buttonState);
        this.button.addEventListener('click', () => this.toggle());
        this.update(device);
    }

    update(device) {
        this.device = device;
        const {name, mac, state: reported} = device;
        this.node.querySelector('.row-name').textContent = name;
        this.node.querySelector('.row-mac').textContent = mac;
        this.node.querySelector('.remove-question').textContent = `Remove ${name}?`;
        this.confirmation.setAttribute('aria-label', `Remove ${name}?`);
        this.input.setAttribute('aria-label', `New name for ${name}`);
        this.renameButton.setAttribute('aria-label', `Rename ${name}`);
        this.removeButton.setAttribute('aria-label', `Remove ${name}`);
        const label = this.node.querySelector('.device-state');
        label.textContent = deviceStates[reported];
        label.dataset.state = reported;

        this.button.title = name;
        this.button.dataset.state = reported;
        this.button.setAttribute('aria-pressed', reported === 'unknown' ? 'mixed' : String(reported === 'on'));
        if (reported === 'unknown') this.button.setAttribute('aria-label', `${name}, state unknown. Refresh state`);
        else this.button.removeAttribute('aria-label');
        this.buttonName.textContent = name;
        this.buttonState.textContent = ` · ${reported === 'unknown' ? '?' : deviceStates[reported]}`;
    }

    showAction(action) {
        this.error.textContent = '';
        this.actions.hidden = true;
        this.renameForm.hidden = action !== 'rename';
        this.confirmation.hidden = action !== 'remove';
        if (action === 'rename') {
            this.input.value = this.device.name;
            this.input.focus();
            this.input.select();
        } else this.node.querySelector('.cancel-remove').focus();
    }

    cancel(button) {
        this.renameForm.hidden = true;
        this.confirmation.hidden = true;
        this.actions.hidden = false;
        this.error.textContent = '';
        if (setupDialog.open) button.focus();
    }

    async rename(event) {
        event.preventDefault();
        if (deviceSetupBusy) return;
        const name = this.input.value.trim();
        if (!name || [...name].length > 40) {
            this.error.textContent = 'Enter a name with 1–40 characters.';
            this.input.focus();
            return;
        }
        const {id} = this.device;
        const saved = await saveDevice(`api/devices/${id}`, {method: 'PATCH', body: {name}}, this.error, device => {
            if (!validDevice(device) || device.id !== id) throw new ApiError('Device response is unavailable.');
            reconcileDevices(devices.map(item => item.id === id ? {...item, name: device.name} : item));
            announce(`Renamed to ${device.name}.`);
        });
        if (saved) this.cancel(this.renameButton);
        else if (setupDialog.open) this.input.focus();
    }

    async remove() {
        if (deviceSetupBusy) return;
        const {id, name} = this.device;
        const saved = await saveDevice(`api/devices/${id}`, {method: 'DELETE'}, this.error, () => {
            reconcileDevices(devices.filter(device => device.id !== id));
            announce(`${name} removed.`);
        });
        if (setupDialog.open) {
            if (!saved) this.node.querySelector('.confirm-remove').focus();
            else if (find('#add-device-fields').disabled) find('#close-setup').focus();
            else deviceMac.focus();
        }
    }

    async toggle() {
        if (this.busy || !devicesSupported) return;
        const {id, state: reported} = this.device;
        const hadFocus = document.activeElement === this.button;
        this.busy = true;
        this.writing = reported !== 'unknown';
        this.button.disabled = true;
        this.button.setAttribute('aria-busy', 'true');
        try {
            if (!this.writing) {
                const error = await loadDevices();
                if (error) throw error;
                return;
            }
            // An overlapping list response may have captured the state before the write.
            deviceRead?.writing.add(id);
            const result = await request(`api/devices/${id}/state`, {method: 'PUT', body: {on: reported === 'off'}});
            if (result?.id !== id || !validDeviceState(result.state)) throw new ApiError('Device state is unavailable.');
            if (deviceViews.get(id) === this) this.setState(result.state);
        } catch (error) {
            if (deviceViews.get(id) === this) {
                this.setState('unknown');
                find('#device-message').textContent = error.message;
                deviceNoticeUntil = performance.now() + 6000;
            }
        } finally {
            this.busy = this.writing = false;
            this.button.disabled = false;
            this.button.removeAttribute('aria-busy');
            if (hadFocus && this.button.isConnected && !setupDialog.open && document.activeElement === document.body) {
                activate();
                this.button.focus();
            }
            if (devices.some(device => device.state === 'unknown')) {
                nextDeviceRead = Math.min(nextDeviceRead, performance.now() + 3000);
            }
        }
    }

    setState(reported) {
        this.device.state = reported;
        this.update(this.device);
    }
}

function reconcileDevices(list) {
    const ids = new Set(list.map(device => device.id));
    for (const [id, view] of deviceViews) {
        if (!ids.has(id)) {
            view.node.remove();
            view.button.remove();
            deviceViews.delete(id);
        }
    }
    devices = list;
    devices.forEach((device, index) => {
        let view = deviceViews.get(device.id);
        if (view) view.update(device);
        else {
            view = new DeviceView(device);
            deviceViews.set(device.id, view);
        }
        placeNode(deviceList, view.node, index);
        placeNode(deviceControls, view.button, index);
    });
    deviceControls.hidden = !devices.length;
    updateDeviceSetup();
}

function updateDeviceSetup() {
    find('#setup-devices').hidden = devicesSupported !== true;
    find('#device-count').textContent = `${devices.length} of ${deviceMax}`;
    find('#add-device-fields').disabled = deviceSetupBusy || devicesSupported !== true || devices.length >= deviceMax;
    find('#manage-devices').disabled = deviceSetupBusy;
}

function loadDevices() {
    if (devicesSupported === false) return Promise.resolve(null);
    if (deviceRead) return deviceRead.promise;
    const revision = deviceSetupRevision;
    const reading = {writing: new Set(devices.filter(device => deviceViews.get(device.id).writing).map(device => device.id))};
    deviceRead = reading;
    reading.promise = (async () => {
        try {
            const data = await request('api/devices');
            if (revision !== deviceSetupRevision) return null;
            if (!data || !Array.isArray(data.devices) || !Number.isInteger(data.max) || data.max < 1 ||
                data.devices.length > data.max || !data.devices.every(validDevice) ||
                new Set(data.devices.map(device => device.id)).size !== data.devices.length) {
                throw new ApiError('Device list is unavailable.');
            }
            devicesSupported = true;
            deviceMax = data.max;
            reconcileDevices(data.devices.map(device => {
                const current = deviceViews.get(device.id)?.device;
                return current && reading.writing.has(device.id) ? {...device, state: current.state} : device;
            }));
            return null;
        } catch (error) {
            if (revision !== deviceSetupRevision) return null;
            if (error.status === 404) {
                devicesSupported = false;
                reconcileDevices([]);
                return null;
            }
            for (const [id, view] of deviceViews) {
                if (!reading.writing.has(id)) view.setState('unknown');
            }
            return error;
        } finally {
            deviceRead = null;
            nextDeviceRead = performance.now() + (devices.some(device => device.state === 'unknown') ? 3000 : 15000);
        }
    })();
    return reading.promise;
}

async function saveDevice(path, options, errorNode, saved) {
    deviceSetupBusy = true;
    updateDeviceSetup();
    errorNode.textContent = '';
    announce('');
    try {
        const result = await request(path, options);
        deviceSetupRevision++;
        saved(result);
        if (deviceRead) await deviceRead.promise;
        await loadDevices();
        return true;
    } catch (error) {
        errorNode.textContent = error.message;
        nextDeviceRead = 0;
        return false;
    } finally {
        deviceSetupBusy = false;
        updateDeviceSetup();
    }
}

async function addDevice(event) {
    event.preventDefault();
    if (find('#add-device-fields').disabled) return;
    const errorNode = find('#add-device-error');
    const mac = normalizeMac(deviceMac.value);
    if (!mac) {
        errorNode.textContent = 'Enter a valid MAC address with six hexadecimal octets.';
        deviceMac.setAttribute('aria-invalid', 'true');
        deviceMac.focus();
        return;
    }
    const name = deviceName.value.trim();
    const progress = find('#add-device-progress');
    progress.hidden = false;
    deviceForm.setAttribute('aria-busy', 'true');
    await saveDevice('api/devices', {method: 'POST', body: name ? {mac, name} : {mac}}, errorNode, device => {
        if (!validDevice(device)) throw new ApiError('Device response is unavailable.');
        reconcileDevices([...devices.filter(item => item.id !== device.id), device]);
        deviceForm.reset();
        deviceMac.removeAttribute('aria-invalid');
        announce(`${device.name} added.`);
    });
    deviceForm.removeAttribute('aria-busy');
    progress.hidden = true;
    if (setupDialog.open && [document.body, setupDialog, deviceMac, deviceName].includes(document.activeElement)) {
        (find('#add-device-fields').disabled ? find('#close-setup') : deviceMac).focus();
    }
}

function deviceTick() {
    const now = performance.now();
    if (deviceNoticeUntil && now >= deviceNoticeUntil) {
        deviceNoticeUntil = 0;
        find('#device-message').textContent = '';
    }
    if (devicesSupported === false || deviceSetupBusy || now < nextDeviceRead) return;
    if (devices.length && devices.every(device => deviceViews.get(device.id).writing)) return;
    loadDevices();
}

function validBackup(data) {
    return data && typeof data.folder === 'string' && Array.isArray(data.cameras) &&
        data.cameras.every(camera => camera && typeof camera.id === 'string' && /^cam[1-9][0-9]{0,3}$/.test(camera.id) &&
            typeof camera.name === 'string' && [0, 1, 7, 30].includes(camera.days) &&
            Object.hasOwn(backupStates, camera.state)) &&
        new Set(data.cameras.map(camera => camera.id)).size === data.cameras.length;
}

function formatBackupSize(bytes) {
    const gb = bytes / 1e9;
    return gb > 1000 ? `${backupSizeFormat.format(gb / 1000)} TB` : `${backupSizeFormat.format(gb)} GB`;
}

function renderBackupSizes() {
    const days = Array.from(backupRows.values(), row => Number(row.picker.value));
    const estimate = days.reduce((sum, value) => sum + value, 0) * 12e9;
    const {free_bytes: free, total_bytes: total, used_bytes: used} = backupData;
    find('#backup-estimate').textContent = `Needs roughly ${formatBackupSize(estimate)}`;
    find('#backup-free').textContent = Number.isFinite(free)
        ? `Free: ${formatBackupSize(free)}${Number.isFinite(total) ? ` of ${formatBackupSize(total)}` : ''}` : '';
    find('#backup-used').textContent = Number.isFinite(used) ? `Used by backups: ${formatBackupSize(used)}` : '';
    find('#backup-space-note').hidden = !Number.isFinite(free) || estimate <= free;
}

function clearBackupConfirmation() {
    pendingBackup = null;
    backupConfirmation.hidden = true;
}

function renderBackup(data, saved = false) {
    if (!validBackup(data)) throw new ApiError('Backup settings are unavailable.');
    const keepDraft = !saved && backupData && (backupFolder.value !== backupData.folder ||
        backupData.cameras.some(({id, days}) => Number(backupRows.get(id).picker.value) !== days));
    if (pendingBackup && (data.folder !== backupData.folder || data.cameras.length !== backupData.cameras.length ||
        data.cameras.some(({id, name, days}, index) => {
            const previous = backupData.cameras[index];
            return id !== previous.id || name !== previous.name || days !== previous.days;
        }))) clearBackupConfirmation();
    if (!backupData) backupError.textContent = '';
    backupData = data;
    backupSection.hidden = false;
    backupFields.disabled = backupSaving;
    if (!keepDraft && backupFolder.value !== data.folder) backupFolder.value = data.folder;

    const ids = new Set(data.cameras.map(camera => camera.id));
    for (const [id, row] of backupRows) {
        if (!ids.has(id)) {
            row.node.remove();
            backupRows.delete(id);
        }
    }
    data.cameras.forEach((camera, index) => {
        let row = backupRows.get(camera.id);
        if (!row) {
            const node = find('#backup-camera-template').content.firstElementChild.cloneNode(true);
            const picker = node.querySelector('select');
            const label = node.querySelector('label');
            const status = node.querySelector('.backup-state');
            const last = node.querySelector('.backup-last');
            picker.id = `backup-retention-${camera.id}`;
            label.htmlFor = picker.id;
            picker.value = String(camera.days);
            row = {node, picker, label, status, last};
            backupRows.set(camera.id, row);
        }
        if (!keepDraft && row.picker.value !== String(camera.days)) row.picker.value = String(camera.days);
        row.label.textContent = camera.name;
        row.status.textContent = backupStates[camera.state];
        row.status.dataset.state = camera.state;
        const lastTime = typeof camera.last === 'string' ? Date.parse(camera.last) : NaN;
        row.last.hidden = !Number.isFinite(lastTime);
        if (!row.last.hidden) {
            row.last.dateTime = camera.last;
            row.last.textContent = `Last file ${backupTimeFormat.format(lastTime)}`;
        }
        placeNode(backupList, row.node, index);
    });
    renderBackupSizes();

    const indicator = find('#backup-indicator');
    const warning = Object.hasOwn(backupWarnings, data.warning) ? backupWarnings[data.warning] : '';
    const label = data.cameras.some(camera => camera.days) ? warning || 'Backup on' : '';
    indicator.hidden = !label;
    indicator.classList.toggle('backup-warning', Boolean(warning));
    if (indicator.textContent !== label) indicator.textContent = label;
}

function hideBackup() {
    clearBackupConfirmation();
    backupSection.hidden = true;
    find('#backup-indicator').hidden = true;
}

async function refreshBackup(fresh = false) {
    if (fresh) clearBackupConfirmation();
    if (backupSaving) {
        if (fresh) lastBackupReadAt = -Infinity;
        return;
    }
    if (backupRead && !fresh) return;
    backupRead?.abort();
    const controller = new AbortController();
    backupRead = controller;
    lastBackupReadAt = performance.now();
    try {
        const data = await request('api/backup', {signal: controller.signal});
        if (backupRead === controller) renderBackup(data);
    } catch (error) {
        if (backupRead !== controller) return;
        if (error.status === 404) hideBackup();
        else if (!backupData) backupError.textContent = error.message;
    } finally {
        if (backupRead === controller) backupRead = null;
    }
}

function backupTick() {
    const delay = setupDialog.open ? 15000 : 60000;
    if (performance.now() - lastBackupReadAt >= delay) refreshBackup();
}

function validateBackupFolder() {
    const missing = !backupFolder.value.trim() && Array.from(backupRows.values()).some(row => Number(row.picker.value));
    backupError.textContent = missing ? 'Enter a storage folder first.' : '';
    if (missing) backupFolder.setAttribute('aria-invalid', 'true');
    else backupFolder.removeAttribute('aria-invalid');
    return !missing;
}

function editBackup() {
    clearBackupConfirmation();
    validateBackupFolder();
    renderBackupSizes();
}

function submitBackup(event) {
    event.preventDefault();
    if (!backupData || backupSaving || backupSection.hidden) return;
    if (!validateBackupFolder()) {
        backupFolder.focus();
        return;
    }
    const cameras = Object.fromEntries(backupData.cameras.map(({id}) => [id, Number(backupRows.get(id).picker.value)]));
    const body = {folder: backupFolder.value, cameras};
    const shortening = backupData.cameras.filter(({id, days}) => cameras[id] > 0 && cameras[id] < days);
    if (!shortening.length) {
        saveBackup(body);
        return;
    }
    const notices = shortening.map(({id, name}) => {
        const days = cameras[id];
        const notice = document.createElement('p');
        notice.textContent = `${name} will keep only ${days} ${days === 1 ? 'day' : 'days'}. Older recordings are deleted now.`;
        return notice;
    });
    find('#backup-confirmation-text').replaceChildren(...notices);
    pendingBackup = body;
    backupConfirmation.hidden = false;
    find('#backup-cancel').focus();
}

async function saveBackup(body) {
    if (backupSaving || backupSection.hidden) return;
    const restoreFocus = backupForm.contains(document.activeElement);
    backupSaving = true;
    backupRead?.abort();
    backupRead = null;
    clearBackupConfirmation();
    backupError.textContent = '';
    backupFields.disabled = true;
    backupForm.setAttribute('aria-busy', 'true');
    backupSave.textContent = 'Saving backup settings…';
    try {
        const data = await request('api/backup', {method: 'PUT', body});
        renderBackup(data, true);
        announce('Backup settings saved.');
    } catch (error) {
        if (error.status === 404) hideBackup();
        else backupError.textContent = error.message;
    } finally {
        backupSaving = false;
        backupFields.disabled = false;
        backupForm.removeAttribute('aria-busy');
        backupSave.textContent = 'Save backup settings';
        if (restoreFocus && setupDialog.open && !backupSection.hidden && document.activeElement === document.body) backupSave.focus();
    }
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
        const tile = tiles.get(camera.id);
        const feedIndex = String(index + 1).padStart(2, '0');
        tile.card.dataset.index = feedIndex;
        tile.label.dataset.index = feedIndex;
        placeNode(grid, tiles.get(camera.id).card, index);
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

function layoutGeometry(layout, orientation, tileCount = 0) {
    const portrait = orientation === 'portrait';
    if (layout === 'spotlight') {
        const columns = 2;
        const rows = portrait ? 3 : 4;
        const capacity = portrait ? columns * (rows - 1) : (columns - 1) * rows;
        return {columns, rows, capacity, scrolling: false};
    }
    if (layout === 'auto') {
        let columns;
        let rows;
        const count = Math.max(1, tileCount);
        if (portrait && count <= 4) { columns = 1; rows = count; }
        else if (count === 1) { columns = 1; rows = 1; }
        else if (count === 2) { columns = 2; rows = 1; }
        else if (count <= 4) { columns = 2; rows = 2; }
        else if (count <= 6) { columns = portrait ? 2 : 3; rows = portrait ? 3 : 2; }
        else if (count <= (portrait ? 10 : 9)) { columns = portrait ? 2 : 3; rows = portrait ? 5 : 3; }
        else return {columns: 1, rows: 1, capacity: count, scrolling: true};
        return {columns, rows, capacity: portrait ? columns * rows : count, scrolling: false};
    }
    const [columns, rows] = portrait
        ? {'2x1': [1, 2], '2x2': [1, 4], '3x3': [2, 5], '4x4': [2, 8]}[layout]
        : layout.split('x').map(Number);
    return {columns, rows, capacity: columns * rows, scrolling: false};
}

function renderPage() {
    const layout = effectiveLayout();
    const orientation = portraitDisplay.matches ? 'portrait' : 'landscape';
    dashboard.dataset.orientation = orientation;
    const spotlight = layout === 'spotlight';
    if (spotlight && grid.dataset.layout !== 'spotlight') resetRotation();
    if (!state.cameras.some(camera => camera.id === state.spotlight)) state.spotlight = state.cameras[0]?.id ?? null;
    const others = spotlight ? state.cameras.filter(camera => camera.id !== state.spotlight) : state.cameras;
    const info = state.weatherEnabled && state.cameras.length > 0;
    const geometry = layoutGeometry(layout, orientation, state.cameras.length + (info ? 1 : 0));
    const slots = Math.max(1, geometry.capacity - (info ? 1 : 0));
    const boundedAuto = layout === 'auto' && orientation === 'portrait' && !geometry.scrolling;
    state.pageCount = Math.max(1, Math.ceil(others.length / slots));
    state.page = Math.max(0, Math.min(state.page, state.pageCount - 1));
    const visible = others.slice(state.page * slots, (state.page + 1) * slots);
    if (spotlight && state.cameras.length) visible.unshift(state.cameras.find(camera => camera.id === state.spotlight));
    state.visible = visible.map(camera => camera.id);
    const visibleIds = new Set(state.visible);
    for (const [id, tile] of tiles) {
        tile.setRole(spotlight && id === state.spotlight, spotlight && id !== state.spotlight);
        if (!visibleIds.has(id)) tile.setVisible(false);
    }
    visible.forEach(camera => tiles.get(camera.id).setVisible(true));
    const placeholders = [...grid.querySelectorAll('.empty-slot')];
    const emptyCount = !spotlight && (layout !== 'auto' || boundedAuto) && state.cameras.length
        ? slots - visible.length : 0;
    // Reuse placeholders too: orientation and page changes never reinsert a tile.
    for (let index = 0; index < emptyCount; index++) {
        if (!placeholders[index]) {
            const placeholder = document.createElement('div');
            placeholder.className = 'empty-slot';
            placeholder.textContent = 'Empty';
            const frame = document.createElement('span');
            frame.className = 'tile-frame';
            frame.setAttribute('aria-hidden', 'true');
            placeholder.appendChild(frame);
            grid.appendChild(placeholder);
            placeholders.push(placeholder);
        }
    }
    placeholders.forEach((placeholder, index) => { placeholder.hidden = index >= emptyCount; });
    grid.dataset.layout = layout;
    grid.dataset.scrolling = String(geometry.scrolling);
    grid.style.setProperty('--grid-columns', String(geometry.columns));
    grid.style.setProperty('--grid-rows', String(geometry.rows));
    infoTile.hidden = !info;
    infoTile.classList.toggle('spotlight-strip', spotlight);
    infoTile.style.setProperty('grid-column', layout === 'auto' && !boundedAuto ? 'auto' : String(geometry.columns));
    infoTile.style.setProperty('grid-row', layout === 'auto' && !boundedAuto ? 'auto' : String(geometry.rows));
    grid.classList.toggle('spotlight-single', spotlight && state.cameras.length === 1 && !info);
    dashboard.dataset.effectiveLayout = layout;
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
    updateActivity();
}

function updateSummary() {
    const counts = {live: 0, offline: 0, connecting: 0};
    state.visible.forEach(id => {
        const status = tiles.get(id)?.state;
        if (status === 'Live') counts.live++;
        else if (status === 'Offline' || status === 'Unavailable') counts.offline++;
        else if (status === 'Connecting' || status === 'Reconnecting') counts.connecting++;
    });
    const groups = Object.entries(counts).filter(([status, count]) => status === 'live' || count > 0);
    const text = groups.map(([status, count]) => `● ${count} ${status}`).join(' ');
    if (summary.textContent !== text) {
        const nodes = [];
        groups.forEach(([status, count], index) => {
            if (index) nodes.push(document.createTextNode(' '));
            const span = document.createElement('span');
            span.className = `health-${status}`;
            span.textContent = `● ${count} ${status}`;
            nodes.push(span);
        });
        summary.replaceChildren(...nodes);
    }
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
            state.loadFailed = false;
            state.max = data.max;
            if (layoutRevision === state.layoutRevision && !state.layoutDirty && !layoutSaving) state.layout = data.layout;
            reconcile(data.cameras);
            find('#connection-message').hidden = true;
            find('#retry-cameras').hidden = true;
            scheduleLoad(30000);
            return true;
        } catch (error) {
            state.loadFailed = true;
            updateActivity();
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
        refreshBackup(true);
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
    find('#weather-error').textContent = '';
    loadWeatherSettings();
    refreshBackup(true);
    activate();
    (find('#add-fields').disabled ? find('#close-setup') : macInput).focus();
}

function changePage(delta) {
    state.page = Math.max(0, Math.min(state.page + delta, state.pageCount - 1));
    renderPage();
}

setupButton.addEventListener('click', openSetup);
find('#empty-setup').addEventListener('click', openSetup);
find('#close-setup').addEventListener('click', () => setupDialog.close());
setupDialog.addEventListener('close', () => {
    clearBackupConfirmation();
    resetRotation();
    activate();
    setupButton.focus();
});
rotationSelect.value = String(state.rotationSeconds);
rotationSelect.addEventListener('change', changeRotationInterval);
themeSelect.value = initialTheme;
themeSelect.addEventListener('change', () => {
    const value = themeValues.includes(themeSelect.value) ? themeSelect.value : 'default';
    themeSelect.value = value;
    applyTheme(value);
    try { localStorage.setItem('kcd.theme', value); } catch { /* Storage can be disabled. */ }
    announce(`Theme set to ${themeSelect.selectedOptions[0].textContent}.`);
});
weatherForm.addEventListener('submit', saveWeather);
weatherForm.addEventListener('input', () => { weatherFormRevision++; });
backupForm.addEventListener('submit', submitBackup);
backupFolder.addEventListener('input', editBackup);
backupList.addEventListener('change', editBackup);
find('#backup-confirm').addEventListener('click', () => {
    if (pendingBackup) saveBackup(pendingBackup);
});
find('#backup-cancel').addEventListener('click', () => {
    clearBackupConfirmation();
    backupSave.focus();
});
addForm.addEventListener('submit', addCamera);
deviceForm.addEventListener('submit', addDevice);
deviceMac.addEventListener('input', () => {
    deviceMac.removeAttribute('aria-invalid');
    find('#add-device-error').textContent = '';
});
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
portraitDisplay.addEventListener('change', renderPage);
let renderedShape = `${portraitDisplay.matches}|${mobile.matches}`;
for (const event of ['pointerdown', 'keydown', 'wheel', 'touchstart']) {
    document.addEventListener(event, activate, {capture: true, passive: true});
}
document.addEventListener('pointermove', event => {
    const moved = lastPointer && (lastPointer.x !== event.clientX || lastPointer.y !== event.clientY);
    lastPointer = {x: event.clientX, y: event.clientY};
    if (moved) activate();
}, {passive: true});
statusBar.addEventListener('pointerenter', () => { pointerOverBar = true; updateActivity(); });
statusBar.addEventListener('pointerleave', () => { pointerOverBar = false; updateActivity(); });
statusBar.addEventListener('focusin', updateActivity);
statusBar.addEventListener('focusout', () => queueMicrotask(updateActivity));
document.addEventListener('keydown', event => {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || setupDialog.open ||
        (event.target instanceof Element && event.target.closest('input, textarea, select, .device-button, [contenteditable]'))) return;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        if (state.pageCount <= 1) return;
        event.preventDefault();
        changePage(event.key === 'ArrowLeft' ? -1 : 1);
    }
});
document.addEventListener('visibilitychange', () => {
    if (!document.hidden) resetRotation();
    tiles.forEach(tile => tile.visibilityChanged());
    updateSummary();
});
document.addEventListener('fullscreenchange', () => {
    fullscreenTile = null;
    const element = document.fullscreenElement;
    for (const tile of tiles.values()) {
        const active = tile.tile === element;
        tile.tile.classList.toggle('is-fullscreen', active);
        tile.updateFullscreenLabel();
        if (active) fullscreenTile = tile;
    }
    if (!fullscreenTile) return;
    const now = new Date();
    fullscreenTile.renderFullscreenInfo(now);
    fullscreenTile.renderFullscreenForecast(freshWeatherForecast(now.getTime()), displayDate(displayTimeParts(now)));
});

function updateClock() {
    const now = new Date();
    clock.dateTime = now.toISOString();
    clock.textContent = displayFormatters ? displayFormatters.statusTime.format(now)
        : now.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', hourCycle: 'h23'});
    date.dateTime = displayDate(displayTimeParts(now));
    date.textContent = displayFormatters ? displayFormatters.statusDate.format(now)
        : now.toLocaleDateString([], {weekday: 'short', day: 'numeric', month: 'short'});
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
weatherTick();
deviceTick();
backupTick();
updateActivity();
loadCameras();
setInterval(() => {
    updateClock();
    weatherTick();
    deviceTick();
    backupTick();
    dailyReload();
    tiles.forEach(tile => tile.updatePlaybackStatus());
    rotateSpotlight();
    // Safety net: some hosts change the viewport without firing media-query change events.
    const shape = `${portraitDisplay.matches}|${mobile.matches}`;
    if (shape !== renderedShape) { renderedShape = shape; renderPage(); }
    updateSummary();
}, 1000);
