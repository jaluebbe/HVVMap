/* Minimal MapLibre GL JS base map for the HVV live vehicle page. */

const HAMBURG_CENTER = [9.9937, 53.5511]; // MapLibre wants [lon, lat]
const HAMBURG_DEFAULT_ZOOM = 12;

// Optional page overrides (window.HVV_PAGE_CONFIG): center [lon, lat], zoom, modes,
// lineToggles {mode: [{key, color, icon}]} - per-line checkboxes replacing that mode's,
// sourceKeys - restricts the data source choices.
const PAGE_CONFIG = window.HVV_PAGE_CONFIG || {};
const LINE_TOGGLES = PAGE_CONFIG.lineToggles || {};

const map = new maplibregl.Map({
    container: 'map',
    style: '/api/vector/style/positron.json',
    center: PAGE_CONFIG.center || HAMBURG_CENTER,
    zoom: PAGE_CONFIG.zoom ?? HAMBURG_DEFAULT_ZOOM,
    minZoom: 7,
    maxZoom: 18,
    attributionControl: false, // added manually below, with our own extra links
});

// visualizePitch: shows a tilt indicator and resets pitch on click
map.addControl(
    new maplibregl.NavigationControl({ visualizePitch: true }),
    'top-left',
);
map.addControl(
    new maplibregl.GeolocateControl({
        positionOptions: { enableHighAccuracy: true },
        trackUserLocation: true,
        showUserHeading: true,
    }),
    'top-left',
);
map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');

const attributionControl = new maplibregl.AttributionControl({
    compact: true,
    customAttribution: [
        '<a href="https://github.com/jaluebbe/HVVMap" target="_blank">Source on GitHub</a>',
        '<a href="https://www.hvv.de/" target="_blank">Fahrplandaten: Hamburger Verkehrsverbund GmbH</a>',
    ],
});
map.addControl(attributionControl, 'bottom-right');

function addPrivacyStatement() {
    fetch('/static/datenschutz.html', { method: 'HEAD' }).then(function(response) {
        if (!response.ok) {
            return;
        }
        const container = map.getContainer().querySelector('.maplibregl-ctrl-attrib-inner');
        if (!container) {
            return;
        }
        container.insertAdjacentHTML(
            'beforeend',
            ' · <a href="/static/datenschutz.html" target="_blank">Impressum &amp; Datenschutzerkl&auml;rung</a>',
        );
    }).catch(function() {});
}
map.on('load', addPrivacyStatement);

function fixIOSResize() {
    setTimeout(() => {
        map.resize();
    }, 250);
}
window.addEventListener('orientationchange', fixIOSResize);
window.addEventListener('resize', fixIOSResize);

// Toggle visibility of vehicle destination labels.
class LabelToggleControl {
    onAdd(mapInstance) {
        this._map = mapInstance;
        this._labelsVisible = true;
        const container = document.createElement('div');
        container.className = 'maplibregl-ctrl maplibregl-ctrl-group';
        const button = document.createElement('button');
        button.type = 'button';
        button.title = 'Ziel-Labels ein-/ausblenden';
        button.style.fontSize = '16px';
        button.textContent = '🏷️';
        button.classList.add('hvv-labels-toggle-active');
        button.addEventListener('click', () => {
            this._labelsVisible = !this._labelsVisible;
            mapInstance.getContainer().classList.toggle('hvv-labels-hidden');
            button.classList.toggle('hvv-labels-toggle-active', this._labelsVisible);
        });
        container.appendChild(button);
        this._container = container;
        return container;
    }

    onRemove() {
        this._container.parentNode.removeChild(this._container);
        this._map = undefined;
    }
}

// Keeps the screen awake via the Screen Wake Lock API. The browser releases
// the lock once the tab is backgrounded - re-acquired on return, unless the
// user had turned it off themselves first.
class WakeLockControl {
    onAdd() {
        this._wakeLock = null;
        this._wantWakeLock = false;
        const container = document.createElement('div');
        container.className = 'maplibregl-ctrl maplibregl-ctrl-group';
        const button = document.createElement('button');
        button.type = 'button';
        button.style.fontSize = '16px';
        button.textContent = '☀️';
        this._button = button;

        if (!('wakeLock' in navigator)) {
            button.disabled = true;
            button.title = 'Bildschirm wach halten (vom Browser nicht unterstützt)';
        } else {
            button.title = 'Bildschirm wach halten';
            button.addEventListener('click', () => this._toggle());
            this._visibilityHandler = () => {
                if (document.visibilityState === 'visible' && this._wantWakeLock && !this._wakeLock) {
                    this._acquire();
                }
            };
            document.addEventListener('visibilitychange', this._visibilityHandler);
        }

        container.appendChild(button);
        this._container = container;
        return container;
    }

    async _acquire() {
        try {
            this._wakeLock = await navigator.wakeLock.request('screen');
            this._wakeLock.addEventListener('release', () => {
                this._wakeLock = null;
                this._setButtonActive(false);
            });
            this._setButtonActive(true);
        } catch (error) {
            console.warn('Wake Lock request failed:', error);
            this._wantWakeLock = false;
            this._setButtonActive(false);
        }
    }

    async _toggle() {
        if (this._wakeLock) {
            this._wantWakeLock = false;
            await this._wakeLock.release();
        } else {
            this._wantWakeLock = true;
            await this._acquire();
        }
    }

    _setButtonActive(active) {
        this._button.classList.toggle('hvv-wakelock-active', active);
        this._button.title = active
            ? 'Bildschirm wach halten (aktiv) - klicken zum Beenden'
            : 'Bildschirm wach halten';
    }

    onRemove() {
        if (this._visibilityHandler) {
            document.removeEventListener('visibilitychange', this._visibilityHandler);
        }
        if (this._wakeLock) {
            this._wakeLock.release();
        }
        this._container.parentNode.removeChild(this._container);
    }
}
// Added after the mode selector so it stacks below it (see setupSourcesAndLayers).

// --- Info panel: full description + links for a disruption marker --------
// Independent from the hover popup - the hover text's short summary alone
// isn't enough for the "Infos" category, where a title like
// "Fahrplanänderung" needs the full description to make sense. Stays open
// across marker switches (content is replaced, not closed/reopened) and
// only closes on an explicit map click or map movement.

function _escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

const infoPanel = document.createElement('div');
infoPanel.id = 'hvv-info-panel';
document.body.appendChild(infoPanel);

function hideInfoPanel() {
    infoPanel.classList.remove('hvv-info-panel-open');
}

function showInfoPanel(messages) {
    const linksHtml = (links) =>
        links && links.length > 0
            ? '<div class="hvv-info-links">' +
              links
                  .map(
                      (l) =>
                          `<a href="${_escapeHtml(l.url)}" target="_blank" rel="noopener noreferrer">${_escapeHtml(l.label)}</a>`,
                  )
                  .join('') +
              '</div>'
            : '';
    infoPanel.innerHTML =
        '<button type="button" class="hvv-info-close" aria-label="Schließen">×</button>' +
        messages
            .map(
                (m) => `
                <div class="hvv-info-message">
                    <div class="hvv-info-summary">${_escapeHtml(m.summary)}</div>
                    <div class="hvv-info-description">${_escapeHtml(m.description)}</div>
                    ${linksHtml(m.links)}
                </div>
            `,
            )
            .join('');
    infoPanel.querySelector('.hvv-info-close').addEventListener('click', hideInfoPanel);
    infoPanel.classList.add('hvv-info-panel-open');
}

map.on('click', function(e) {
    if (e.originalEvent && e.originalEvent._hvvHandledByMarker) {
        return;
    }
    hideInfoPanel();
});
map.on('movestart', hideInfoPanel);
map.on('zoomstart', hideInfoPanel);

const HVV_POLL_INTERVAL_MS = 1_000;
const REFERENCE_POLL_INTERVAL_MS = 60_000;
const DISRUPTIONS_POLL_INTERVAL_MS = 60_000;

const EMPTY_FEATURE_COLLECTION = { type: 'FeatureCollection', features: [] };

// Shared by hvv-stops-layer-* and hvv-disruptions-layer-* so both circle kinds match in size.
const STOP_MARKER_RADIUS = 2;

// Replacement-service lines (e.g. "U1-ERSATZ", "A1-SEV", "A3-BUS") - a 2-char
// line name followed by a hyphen - render dashed instead of solid (see refreshReferenceLayer).
const REPLACEMENT_LINE_PATTERN = /^.{2}-/;

// ModeToggleControl toggles a mode's lines/stops/positions together.
const ALL_MODES = [
    { key: 'U', name: 'U-Bahn', icon: '/static/hvv/icons/u.svg' },
    { key: 'S', name: 'S-Bahn', icon: '/static/hvv/icons/s.svg' },
    { key: 'AKN', name: 'AKN', icon: '/static/hvv/icons/akn.svg' },
    { key: 'FERRY', name: 'Fähre', icon: '/static/hvv/icons/ferry.svg' },
    { key: 'R', name: 'Regio', icon: '/static/hvv/icons/r.svg' },
];
const MODES = PAGE_CONFIG.modes ? ALL_MODES.filter((m) => PAGE_CONFIG.modes.includes(m.key)) : ALL_MODES;

// "U1-ERSATZ" -> "U1"
function baseLineName(name) {
    return (name || '').split('-')[0];
}

// SONSTIGE (displayed as "Infos") has no single fitting emoji among the
// obvious closure/accessibility ones, so it gets the info symbol instead -
// matches CATEGORY_COLORS in announcement_categories.py for marker color.
const DISRUPTION_CATEGORIES = [
    { key: 'SPERRUNG', name: 'Sperrung', icon: '🚧', visible: true },
    { key: 'BARRIEREFREIHEIT', name: 'Aufzüge', icon: '♿', visible: true },
    { key: 'SONSTIGE', name: 'Infos', icon: 'ℹ️', visible: true },
];

function buildIconNode(icon) {
    if (/^(\/|https?:)/.test(icon)) {
        const img = document.createElement('img');
        img.src = icon;
        img.height = 16;
        return img;
    }
    const span = document.createElement('span');
    span.textContent = icon;
    return span;
}

// One checkbox per mode + disruption category; collapsible on narrow/short screens.
class ModeToggleControl {
    constructor(onModeToggle, categories, onCategoryToggle, sourceConfig, onLineToggle, extraToggles) {
        this._extraToggles = extraToggles || [];
        this._onModeToggle = onModeToggle;
        this._onLineToggle = onLineToggle;
        this._categories = categories || [];
        this._onCategoryToggle = onCategoryToggle;
        this._sourceConfig = sourceConfig || null;
    }

    _appendItem(container, key, name, icon, checked, onToggle) {
        const label = document.createElement('label');
        label.className = 'hvv-mode-control-item';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = checked;
        checkbox.setAttribute('aria-label', name || key);
        checkbox.addEventListener('change', () => onToggle(key, checkbox.checked));
        label.append(checkbox, buildIconNode(icon), document.createTextNode(name));
        container.appendChild(label);
    }

    onAdd(mapInstance) {
        this._map = mapInstance;
        const container = document.createElement('div');
        container.className = 'maplibregl-ctrl maplibregl-ctrl-group hvv-mode-control';
        // Matches on width OR height so landscape phones still count as mobile.
        const mobileQuery = window.matchMedia('(max-width: 600px), (max-height: 600px)');
        const syncCollapsedState = () => container.classList.toggle('hvv-mode-control-collapsed', mobileQuery.matches);
        syncCollapsedState();
        mobileQuery.addEventListener('change', syncCollapsedState);

        const toggleButton = document.createElement('button');
        toggleButton.type = 'button';
        toggleButton.className = 'hvv-mode-control-toggle';
        toggleButton.title = 'Ebenen ein-/ausklappen';
        toggleButton.style.fontSize = '16px'; // matches LabelToggleControl's icon size
        toggleButton.textContent = '☰';
        toggleButton.addEventListener('click', () => {
            container.classList.toggle('hvv-mode-control-collapsed');
        });
        container.appendChild(toggleButton);

        // Auto-collapse on mobile once the user interacts with the map.
        const collapseOnMapClick = () => {
            if (mobileQuery.matches) {
                container.classList.add('hvv-mode-control-collapsed');
            }
        };
        mapInstance.on('click', collapseOnMapClick);
        this._collapseOnMapClick = collapseOnMapClick;
        this._mobileQuery = mobileQuery;
        this._syncCollapsedState = syncCollapsedState;

        const items = document.createElement('div');
        items.className = 'hvv-mode-control-items';
        MODES.forEach((m) => {
            const lineToggles = LINE_TOGGLES[m.key];
            if (lineToggles) {
                // Icon already names the line.
                lineToggles.forEach((l) => this._appendItem(items, l.key, '', l.icon, true, this._onLineToggle));
            } else {
                this._appendItem(items, m.key, m.name, m.icon, true, this._onModeToggle);
            }
        });

        this._categoriesSection = document.createElement('div');
        this._categoriesSection.className = 'hvv-mode-control-categories';
        if (this._categories.length > 0) {
            const divider = document.createElement('div');
            divider.className = 'hvv-mode-control-divider';
            this._categoriesSection.appendChild(divider);
            this._categories.forEach((c) => {
                this._appendItem(this._categoriesSection, c.key, c.name, c.icon, c.visible, this._onCategoryToggle);
            });
        }
        items.appendChild(this._categoriesSection);

        this._extrasSection = document.createElement('div');
        if (this._extraToggles.length > 0) {
            const extraDivider = document.createElement('div');
            extraDivider.className = 'hvv-mode-control-divider';
            this._extrasSection.appendChild(extraDivider);
            this._extraToggles.forEach((t) => this._appendItem(this._extrasSection, t.key, t.name, t.icon || '', t.checked, t.onToggle));
        }
        items.appendChild(this._extrasSection);

        if (this._sourceConfig && this._sourceConfig.sources.length > 1) {
            const sourceDivider = document.createElement('div');
            sourceDivider.className = 'hvv-mode-control-divider';
            items.appendChild(sourceDivider);

            const select = document.createElement('select');
            select.className = 'hvv-mode-control-source-select';
            this._sourceConfig.sources.forEach((s) => {
                const option = document.createElement('option');
                option.value = s.key;
                option.textContent = s.name;
                option.selected = s.key === this._sourceConfig.initialKey;
                select.appendChild(option);
            });
            select.addEventListener('change', () => this._sourceConfig.onSourceChange(select.value));
            items.appendChild(select);
        }

        container.appendChild(items);

        this._container = container;
        return container;
    }

    // Hides the disruption checkboxes for sources without disruption data (e.g. GTFS).
    setCategoriesVisible(visible) {
        if (this._categoriesSection) {
            this._categoriesSection.style.display = visible ? '' : 'none';
        }
    }

    // Shows the extra toggles (e.g. straight lines) only for sources that use them.
    setExtrasVisible(visible) {
        if (this._extrasSection) {
            this._extrasSection.style.display = visible ? '' : 'none';
        }
    }

    onRemove() {
        this._map.off('click', this._collapseOnMapClick);
        this._mobileQuery.removeEventListener('change', this._syncCollapsedState);
        this._container.parentNode.removeChild(this._container);
        this._map = undefined;
    }
}

// Vehicle class from the first three characters of the popup's second line
// (model or vehicles): "DT5" (DT5 units), "Bat" (Batteriegelenkbus).
const VEHICLE_MODEL_CLASSES = {
    'Bat': 'hvv-vehicle-batteriegelenkbus',
    'DT5': 'hvv-vehicle-dt5',
};

function vehicleModelFromText(text, delay) {
    if (!text) {
        return '';
    }
    const parts = text.split('<br>');
    const lines = delay > 0 ? parts.slice(1, -1) : parts.slice(1);
    return (lines[0] || '').slice(0, 3);
}

function buildMarkerElement(properties) {
    const wrapper = document.createElement('div');
    wrapper.className = 'hvv-marker-wrapper';

    if (properties.icon) {
        const height = properties.iconHeight || 20;
        // Space between icon and label scales with line name length.
        const lineNameLength = (properties.line || '').length || 2;
        const labelOffset = Math.round(height * (0.28 * lineNameLength + 0.9));
        const delay = properties.delay || 0;
        const delayText = delay > 0 ? `+${delay}` : '';
        const delayBadge = delayText ? ` <span class="hvv-marker-delay">${delayText}</span>` : '';
        const modelClass = VEHICLE_MODEL_CLASSES[vehicleModelFromText(properties.text, delay)] || '';
        const labelClass = modelClass ? `hvv-marker-label ${modelClass}` : 'hvv-marker-label';
        const label = properties.label
            ? `<span class="${labelClass}" style="left:${labelOffset}px;">${properties.label}${delayBadge}</span>`
            : '';
        // Stays visible when labels are hidden overall (see CSS).
        const delayStandalone = delayText
            ? `<span class="hvv-marker-delay-standalone" style="left:${labelOffset}px;">${delayText}</span>`
            : '';
        wrapper.innerHTML = `<img src="${properties.icon}" style="height:${height}px" />${label}${delayStandalone}`;

        // Hit area spans icon + estimated label width - doesn't need to be pixel-exact.
        const halfHeight = height / 2;
        const labelWidthEstimate = properties.label ? properties.label.length * 6.5 + 16 : 0;
        const rightEdge = properties.label ? labelOffset + labelWidthEstimate : halfHeight;
        const hitbox = document.createElement('div');
        hitbox.className = 'hvv-marker-hitbox';
        hitbox.style.left = `${-halfHeight}px`;
        hitbox.style.top = `${-Math.max(halfHeight, 10)}px`;
        hitbox.style.width = `${rightEdge + halfHeight}px`;
        hitbox.style.height = `${Math.max(height, 20)}px`;
        wrapper.appendChild(hitbox);
        return wrapper;
    }

    // Fallback for unmapped lines - a plain colored dot, self-centered like the icon above.
    const dot = document.createElement('div');
    const diameter = (properties.markerRadius || 5) * 2;
    dot.style.position = 'absolute';
    dot.style.transform = 'translate(-50%, -50%)';
    dot.style.width = `${diameter}px`;
    dot.style.height = `${diameter}px`;
    dot.style.borderRadius = '50%';
    dot.style.background = properties.color || '#888888';
    wrapper.appendChild(dot);

    const hitbox = document.createElement('div');
    hitbox.className = 'hvv-marker-hitbox';
    hitbox.style.left = `${-diameter / 2}px`;
    hitbox.style.top = `${-diameter / 2}px`;
    hitbox.style.width = `${diameter}px`;
    hitbox.style.height = `${diameter}px`;
    wrapper.appendChild(hitbox);
    return wrapper;
}


// Built-in data sources - GTFS has no disruptions endpoint (null skips that layer).
const ALL_SOURCES = [
    {
        key: 'live',
        name: 'Live',
        positionsEndpoint: '/api/hvv/live/positions.geojson',
        linesEndpoint: '/api/hvv/live/lines.geojson',
        stopsEndpoint: '/api/hvv/live/stops.geojson',
        disruptionsEndpoint: '/api/hvv/live/disruptions.geojson',
    },
    {
        key: 'realtime',
        name: 'Realtime',
        positionsEndpoint: '/api/hvv/realtime/positions.geojson',
        linesEndpoint: '/api/hvv/live/lines.geojson',
        stopsEndpoint: '/api/hvv/live/stops.geojson',
        disruptionsEndpoint: '/api/hvv/live/disruptions.geojson',
    },
    {
        key: 'gtfs',
        name: 'GTFS',
        positionsEndpoint: '/api/hvv/gtfs/positions.geojson',
        linesEndpoint: '/api/hvv/gtfs/lines.geojson',
        stopsEndpoint: '/api/hvv/gtfs/stops.geojson',
        disruptionsEndpoint: null,
        // Shapes and segments without a track (properties.straight) get a "Luftlinien" toggle.
        straightToggle: true,
    },
    {
        // departureList/sublines based, vehicles computed from the trip table.
        key: 'gtilive',
        name: 'GTI-Live',
        // Hops without a stored track (properties.straight) get a "Luftlinien" toggle.
        straightToggle: true,
        positionsEndpoint: '/api/hvv/gtilive/positions.geojson',
        linesEndpoint: '/api/hvv/gtilive/lines.geojson',
        stopsEndpoint: '/api/hvv/gtilive/stops.geojson',
        disruptionsEndpoint: '/api/hvv/live/disruptions.geojson',
    },
];
const SOURCES = PAGE_CONFIG.sourceKeys
    ? ALL_SOURCES.filter((s) => PAGE_CONFIG.sourceKeys.includes(s.key))
    : ALL_SOURCES;

// initialSourceKey defaults to 'live' when omitted or unknown.
function initHvvVehicleLayers(initialSourceKey) {
    // Single shared popup (vehicles/stops/disruptions); anchor:'bottom' points its tip straight down.
    const hoverPopup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, anchor: 'bottom' });
    const activeModes = new Set(MODES.map((m) => m.key));
    const activeCategories = new Set(DISRUPTION_CATEGORIES.filter((c) => c.visible).map((c) => c.key));
    const positionMarkersByMode = {};
    MODES.forEach((m) => { positionMarkersByMode[m.key] = []; });
    // Markers by journeyID, kept across polls and moved in place.
    const positionEntries = new Map();
    let hoveredPositionKey = null;
    let currentSource = SOURCES.find((s) => s.key === initialSourceKey) || SOURCES[0];
    let modeToggleControl = null;
    let showStraight = false;
    const pollTimers = [];
    const activeLines = new Set(Object.values(LINE_TOGGLES).flat().map((l) => l.key));
    // Last fetched lines/stops, re-filtered on line toggle.
    const referenceData = {};

    // Modes without line toggles and features without line info always pass.
    function linesVisible(modeKey, lineNames) {
        if (!LINE_TOGGLES[modeKey] || !lineNames || lineNames.length === 0) {
            return true;
        }
        return lineNames.some((name) => activeLines.has(baseLineName(name)));
    }

    // Vehicles on a straight station-to-station segment follow the straight-line toggle.
    function markerVisible(modeKey, marker) {
        return activeModes.has(modeKey)
            && linesVisible(modeKey, [marker.hvvLine])
            && (showStraight || !marker.hvvStraight);
    }

    function updateMarkerVisibility() {
        MODES.forEach((m) => {
            positionMarkersByMode[m.key].forEach((marker) => {
                marker.getElement().style.display = markerVisible(m.key, marker) ? '' : 'none';
            });
        });
    }

    // Restricted pages hide disruptions without a mode.
    function disruptionVisible(properties) {
        const modes = properties?.modes || [];
        if (modes.length === 0) {
            return !PAGE_CONFIG.modes;
        }
        return modes.some((mode) => activeModes.has(mode) && linesVisible(mode, properties.lines));
    }

    function setModeVisibility(modeKey, visible) {
        if (visible) {
            activeModes.add(modeKey);
        } else {
            activeModes.delete(modeKey);
        }
        const visibility = visible ? 'visible' : 'none';
        map.setLayoutProperty(`hvv-lines-layer-${modeKey}`, 'visibility', visibility);
        map.setLayoutProperty(`hvv-lines-layer-${modeKey}-replacement`, 'visibility', visibility);
        map.setLayoutProperty(`hvv-lines-layer-${modeKey}-straight`, 'visibility', visibility);
        map.setLayoutProperty(`hvv-stops-layer-${modeKey}`, 'visibility', visibility);
        map.setLayoutProperty(`hvv-stops-labels-${modeKey}`, 'visibility', visibility);
        positionMarkersByMode[modeKey].forEach((marker) => {
            marker.getElement().style.display = markerVisible(modeKey, marker) ? '' : 'none';
        });
        renderDisruptionLayers();
    }

    function setLineVisibility(lineKey, visible) {
        if (visible) {
            activeLines.add(lineKey);
        } else {
            activeLines.delete(lineKey);
        }
        updateMarkerVisibility();
        renderReferenceLayer('hvv-lines');
        renderReferenceLayer('hvv-stops');
        renderDisruptionLayers();
    }

    function setStraightVisibility(_key, visible) {
        showStraight = visible;
        updateMarkerVisibility();
        renderReferenceLayer('hvv-lines');
    }

    function setCategoryVisibility(categoryKey, visible) {
        if (visible) {
            activeCategories.add(categoryKey);
        } else {
            activeCategories.delete(categoryKey);
        }
        map.setLayoutProperty(`hvv-disruptions-layer-${categoryKey}`, 'visibility', visible ? 'visible' : 'none');
        updateStationDisruptions();
    }

    function setupSourcesAndLayers() {
        const hoverLayers = [];
        const disruptionLayerIds = [];
        // Split into two passes (lines+dots, then labels) so disruption markers can be
        // inserted in between - above every mode's stop dots, below every mode's labels.
        MODES.forEach(function(m) {
            map.addSource(`hvv-lines-${m.key}`, { type: 'geojson', data: EMPTY_FEATURE_COLLECTION });
            map.addLayer({
                id: `hvv-lines-layer-${m.key}`,
                type: 'line',
                source: `hvv-lines-${m.key}`,
                filter: ['all', ['!', ['get', 'isReplacementLine']], ['!', ['coalesce', ['get', 'straight'], false]]],
                paint: {
                    'line-color': ['coalesce', ['get', 'color'], '#888888'],
                    'line-width': 1.5,
                },
            });
            // line-dasharray isn't data-driven in MapLibre, hence a separate layer
            // (same source, filtered) instead of one expression-based layer.
            map.addLayer({
                id: `hvv-lines-layer-${m.key}-replacement`,
                type: 'line',
                source: `hvv-lines-${m.key}`,
                filter: ['all', ['==', ['get', 'isReplacementLine'], true], ['!', ['coalesce', ['get', 'straight'], false]]],
                paint: {
                    'line-color': ['coalesce', ['get', 'color'], '#888888'],
                    'line-width': 1.5,
                    'line-dasharray': [1, 3],
                },
            });

            // Hops without a stored track; only present while the straight-line toggle is on.
            map.addLayer({
                id: `hvv-lines-layer-${m.key}-straight`,
                type: 'line',
                source: `hvv-lines-${m.key}`,
                filter: ['==', ['coalesce', ['get', 'straight'], false], true],
                paint: {
                    'line-color': ['coalesce', ['get', 'color'], '#888888'],
                    'line-width': 1.5,
                    'line-opacity': 0.7,
                    'line-dasharray': [2, 2],
                },
            });

            map.addSource(`hvv-stops-${m.key}`, { type: 'geojson', data: EMPTY_FEATURE_COLLECTION });
            map.addLayer({
                id: `hvv-stops-layer-${m.key}`,
                type: 'circle',
                source: `hvv-stops-${m.key}`,
                paint: {
                    'circle-radius': STOP_MARKER_RADIUS,
                    'circle-color': '#ffffff',
                    'circle-stroke-color': '#000000',
                    'circle-stroke-width': 1,
                },
            });
            // Backup for stops whose label (below) doesn't fit/render for
            // some reason - cheap to keep, works regardless.
            hoverLayers.push(`hvv-stops-layer-${m.key}`);
        });

        // Always created regardless of the current source, so switching sources doesn't need new layers.
        // Same radius as the stop dots, but added after them so they render in front.
        DISRUPTION_CATEGORIES.forEach(function(c) {
            map.addSource(`hvv-disruptions-${c.key}`, { type: 'geojson', data: EMPTY_FEATURE_COLLECTION });
            map.addLayer({
                id: `hvv-disruptions-layer-${c.key}`,
                type: 'circle',
                source: `hvv-disruptions-${c.key}`,
                layout: { visibility: c.visible ? 'visible' : 'none' },
                paint: {
                    'circle-radius': STOP_MARKER_RADIUS,
                    'circle-color': ['coalesce', ['get', 'color'], '#888888'],
                },
            });
            hoverLayers.push(`hvv-disruptions-layer-${c.key}`);
            disruptionLayerIds.push(`hvv-disruptions-layer-${c.key}`);
        });

        MODES.forEach(function(m) {
            // Our own station/pier name labels, from our own stops data.
            map.addLayer({
                id: `hvv-stops-labels-${m.key}`,
                type: 'symbol',
                source: `hvv-stops-${m.key}`,
                minzoom: 8.5,
                layout: {
                    'text-field': ['get', 'name'],
                    'text-font': ['KlokanTech Noto Sans Bold'],
                    'text-size': 9.5,
                    'text-anchor': 'center',
                    'text-max-width': 14,
                    'text-padding': 2,
                    'text-letter-spacing': 0.05, // Prevents bold characters from bleeding together
                    'text-allow-overlap': false, // Hides overlapping labels to keep the map clean
                },
                paint: {
                    'text-color': 'hsl(232, 41%, 48%)',
                    'text-halo-color': 'hsl(0, 0%, 100%)',
                    'text-halo-width': 0.8,
                    'text-translate': [0, 1],
                    'text-translate-anchor': 'viewport',
                },
            });
        });

        // Hover popup for the vehicle, stop and disruption layers.
        hoverLayers.forEach(function(layerId) {
            map.on('mouseenter', layerId, function(e) {
                map.getCanvas().style.cursor = 'pointer';
                const feature = e.features[0];
                const text = feature.properties && feature.properties.text;
                if (text) {
                    hoverPopup.setLngLat(feature.geometry.coordinates).setHTML(text).addTo(map);
                }
            });
            map.on('mouseleave', layerId, function() {
                map.getCanvas().style.cursor = '';
                hoverPopup.remove();
            });
        });

        // Station labels share the same tooltip/click-to-open-info-panel as the
        // disruption marker itself, looked up by station name (see updateStationDisruptions).
        MODES.forEach(function(m) {
            const layerId = `hvv-stops-labels-${m.key}`;
            map.on('mouseenter', layerId, function(e) {
                const info = stationDisruptionInfo.get(e.features[0].properties?.name);
                if (!info) {
                    return;
                }
                map.getCanvas().style.cursor = 'pointer';
                hoverPopup.setLngLat(e.features[0].geometry.coordinates).setHTML(info.text).addTo(map);
            });
            map.on('mouseleave', layerId, function() {
                map.getCanvas().style.cursor = '';
                hoverPopup.remove();
            });
            map.on('click', layerId, function(e) {
                const info = stationDisruptionInfo.get(e.features[0].properties?.name);
                if (!info || info.messages.length === 0) {
                    return;
                }
                showInfoPanel(info.messages);
                e.originalEvent._hvvHandledByMarker = true;
            });
        });

        // Separate from the hover popup above: opens the bottom info panel
        // with the full description/links, which the short hover text
        // can't carry. GL serializes nested properties (our "messages"
        // array) to a JSON string on the feature - parse it back.
        disruptionLayerIds.forEach(function(layerId) {
            map.on('click', layerId, function(e) {
                const feature = e.features[0];
                const raw = feature.properties && feature.properties.messages;
                const messages = typeof raw === 'string' ? JSON.parse(raw) : raw;
                if (messages && messages.length > 0) {
                    showInfoPanel(messages);
                    e.originalEvent._hvvHandledByMarker = true;
                }
            });
        });

        modeToggleControl = new ModeToggleControl(
            setModeVisibility,
            DISRUPTION_CATEGORIES,
            setCategoryVisibility,
            { sources: SOURCES, initialKey: currentSource.key, onSourceChange: switchSource },
            setLineVisibility,
            [{ key: 'straight', name: 'Luftlinien', icon: '⋯', checked: false, onToggle: setStraightVisibility }],
        );
        map.addControl(modeToggleControl, 'top-right');
        modeToggleControl.setCategoriesVisible(!!currentSource.disruptionsEndpoint);
        modeToggleControl.setExtrasVisible(!!currentSource.straightToggle);
        map.addControl(new LabelToggleControl(), 'top-right');
        map.addControl(new WakeLockControl(), 'top-right');
    }

    function activeLineColor(modeKey, lineNames) {
        const toggles = LINE_TOGGLES[modeKey];
        if (!toggles || !lineNames) {
            return null;
        }
        for (const name of lineNames) {
            const toggle = toggles.find((l) => l.key === baseLineName(name));
            if (toggle && toggle.color && activeLines.has(toggle.key)) {
                return toggle.color;
            }
        }
        return null;
    }

    function renderReferenceLayer(sourcePrefix) {
        const data = referenceData[sourcePrefix];
        if (!data) {
            return;
        }
        MODES.forEach(function(m) {
            const filtered = {
                type: 'FeatureCollection',
                features: data.features
                    .filter((f) => (f.properties?.modes || []).includes(m.key))
                    .filter((f) => linesVisible(m.key, f.properties?.lines))
                    .filter((f) => showStraight || !f.properties?.straight)
                    .map((f) => ({
                        ...f,
                        properties: {
                            ...f.properties,
                            ...(sourcePrefix === 'hvv-lines' && activeLineColor(m.key, f.properties?.lines)
                                ? { color: activeLineColor(m.key, f.properties.lines) }
                                : {}),
                            isReplacementLine: (f.properties?.lines || []).some((name) => REPLACEMENT_LINE_PATTERN.test(name)),
                        },
                    })),
            };
            map.getSource(`${sourcePrefix}-${m.key}`).setData(filtered);
        });
    }

    async function refreshReferenceLayer(endpoint, sourcePrefix, layerName) {
        try {
            const response = await fetch(endpoint, { cache: 'no-store' });
            if (!response.ok) {
                console.warn(`HVV ${layerName} layer: HTTP`, response.status);
                return;
            }
            referenceData[sourcePrefix] = await response.json();
            renderReferenceLayer(sourcePrefix);
        } catch (error) {
            console.warn(`Failed to load HVV ${layerName} layer:`, error);
        }
    }

    async function refreshHvvPositions() {
        if (!currentSource.positionsEndpoint) {
            return;
        }
        try {
            const response = await fetch(currentSource.positionsEndpoint, { cache: 'no-store' });
            if (!response.ok) {
                console.warn('HVV positions: HTTP', response.status);
                return;
            }
            const data = await response.json();
            const seen = new Set();
            data.features.forEach(function(feature, index) {
                const properties = feature.properties || {};
                const modeKey = properties.mode;
                if (!positionMarkersByMode[modeKey]) {
                    return; // unknown mode - shouldn't happen, but don't crash the poll
                }
                let key = properties.journeyID || `#${index}`;
                if (seen.has(key)) {
                    key = `${key}|${index}`; // duplicate id within one poll
                }
                seen.add(key);
                // Everything buildMarkerElement renders; a change rebuilds the marker.
                const look = JSON.stringify([modeKey, properties.icon, properties.iconHeight,
                    properties.line, properties.label, properties.delay, properties.text]);
                let entry = positionEntries.get(key);
                if (entry && entry.look === look) {
                    entry.marker.setLngLat(feature.geometry.coordinates);
                    if (hoveredPositionKey === key) {
                        hoverPopup.setLngLat(feature.geometry.coordinates);
                    }
                } else {
                    if (entry) {
                        entry.marker.remove();
                    }
                    const element = buildMarkerElement(properties);
                    // anchor 'top-left'; the icon re-centers via CSS transform.
                    const marker = new maplibregl.Marker({ element, anchor: 'top-left' })
                        .setLngLat(feature.geometry.coordinates)
                        .addTo(map);
                    marker.hvvLine = properties.line;
                    entry = { marker, look, modeKey };
                    positionEntries.set(key, entry);
                    if (properties.text) {
                        // Real DOM element, so hover wires up directly (no map layer events needed).
                        element.addEventListener('mouseenter', function() {
                            hoveredPositionKey = key;
                            // Extra gap above the icon so the popup doesn't sit flush against it.
                            const halfIconHeight = (properties.iconHeight || 20) / 2;
                            hoverPopup
                                .setOffset(halfIconHeight)
                                .setLngLat(entry.marker.getLngLat())
                                .setHTML(properties.text)
                                .addTo(map);
                        });
                        element.addEventListener('mouseleave', function() {
                            hoveredPositionKey = null;
                            hoverPopup.remove();
                        });
                    }
                }
                entry.marker.hvvStraight = Boolean(properties.straight);
            });
            positionEntries.forEach((entry, key) => {
                if (!seen.has(key)) {
                    if (hoveredPositionKey === key) {
                        hoveredPositionKey = null;
                        hoverPopup.remove();
                    }
                    entry.marker.remove();
                    positionEntries.delete(key);
                }
            });
            MODES.forEach((m) => { positionMarkersByMode[m.key] = []; });
            positionEntries.forEach((entry) => {
                positionMarkersByMode[entry.modeKey].push(entry.marker);
            });
            updateMarkerVisibility();
        } catch (error) {
            console.warn('Failed to load HVV positions:', error);
        }
    }

    // Kept for re-filtering on mode toggle without re-fetching.
    let disruptionRawData = null;

    function renderDisruptionLayers() {
        if (!disruptionRawData) {
            return;
        }
        DISRUPTION_CATEGORIES.forEach(function(c) {
            const source = map.getSource(`hvv-disruptions-${c.key}`);
            if (!source) {
                return;
            }
            const filtered = {
                type: 'FeatureCollection',
                // Kept in sync regardless of visibility, so toggling a category shows data immediately.
                features: disruptionRawData.features.filter(function(f) {
                    return f.properties?.category === c.key && disruptionVisible(f.properties);
                }),
            };
            source.setData(filtered);
        });
        updateStationDisruptions();
    }

    // Matches CATEGORY_COLORS in announcement_categories.py; higher priority wins if a
    // station has several active categories at once.
    const STATION_LABEL_COLORS = {
        SPERRUNG: '#e65100',
        BARRIEREFREIHEIT: '#42A5F5',
        SONSTIGE: '#616161',
    };
    const STATION_LABEL_DEFAULT_COLOR = 'hsl(232, 41%, 48%)';
    const STATION_LABEL_CATEGORY_PRIORITY = ['SPERRUNG', 'BARRIEREFREIHEIT', 'SONSTIGE'];

    // station name -> {text, messages}, combining every active disruption feature
    // there - read by the label hover/click handlers registered in setupSourcesAndLayers.
    let stationDisruptionInfo = new Map();

    // Colors each hvv-stops-labels-* layer's text by the affected station's active
    // disruption category, and rebuilds stationDisruptionInfo for the label tooltip/click.
    function updateStationDisruptions() {
        const colorByStation = {};
        const infoByStation = new Map();
        (disruptionRawData?.features || []).forEach(function(f) {
            const category = f.properties?.category;
            const stationName = f.properties?.station_name;
            if (!category || !stationName || !activeCategories.has(category)) {
                return;
            }
            if (!disruptionVisible(f.properties)) {
                return;
            }
            if (STATION_LABEL_COLORS[category]) {
                const existing = colorByStation[stationName];
                if (!existing || STATION_LABEL_CATEGORY_PRIORITY.indexOf(category) < STATION_LABEL_CATEGORY_PRIORITY.indexOf(existing)) {
                    colorByStation[stationName] = category;
                }
            }
            const entry = infoByStation.get(stationName) || { texts: [], messages: [] };
            if (f.properties.text) {
                entry.texts.push(f.properties.text);
            }
            const rawMessages = f.properties.messages;
            const messages = typeof rawMessages === 'string' ? JSON.parse(rawMessages) : rawMessages;
            if (Array.isArray(messages)) {
                entry.messages.push(...messages);
            }
            infoByStation.set(stationName, entry);
        });

        stationDisruptionInfo = new Map(
            Array.from(infoByStation, ([name, entry]) => [name, { text: entry.texts.join('<br><br>'), messages: entry.messages }]),
        );

        const stationNames = Object.keys(colorByStation);
        const textColor = stationNames.length === 0
            ? STATION_LABEL_DEFAULT_COLOR
            : ['match', ['get', 'name'], ...stationNames.flatMap((name) => [name, STATION_LABEL_COLORS[colorByStation[name]]]), STATION_LABEL_DEFAULT_COLOR];
        MODES.forEach((m) => map.setPaintProperty(`hvv-stops-labels-${m.key}`, 'text-color', textColor));
    }

    async function refreshDisruptions() {
        try {
            const response = await fetch(currentSource.disruptionsEndpoint, { cache: 'no-store' });
            if (!response.ok) {
                console.warn('HVV disruptions: HTTP', response.status);
                return;
            }
            disruptionRawData = await response.json();
            renderDisruptionLayers();
        } catch (error) {
            console.warn('Failed to load HVV disruptions:', error);
        }
    }

    function stopPolling() {
        pollTimers.forEach((id) => clearInterval(id));
        pollTimers.length = 0;
    }

    // Wipes markers/sources so stale data from the previous source doesn't linger.
    function clearAllData() {
        Object.keys(referenceData).forEach((key) => delete referenceData[key]);
        MODES.forEach((m) => {
            map.getSource(`hvv-lines-${m.key}`).setData(EMPTY_FEATURE_COLLECTION);
            map.getSource(`hvv-stops-${m.key}`).setData(EMPTY_FEATURE_COLLECTION);
            positionMarkersByMode[m.key].forEach((marker) => marker.remove());
            positionMarkersByMode[m.key] = [];
        });
        positionEntries.clear();
        hoveredPositionKey = null;
        disruptionRawData = null;
        DISRUPTION_CATEGORIES.forEach((c) => {
            map.getSource(`hvv-disruptions-${c.key}`).setData(EMPTY_FEATURE_COLLECTION);
        });
        updateStationDisruptions();
    }

    function startPolling() {
        const refreshLines = () => refreshReferenceLayer(currentSource.linesEndpoint, 'hvv-lines', 'lines');
        const refreshStops = () => refreshReferenceLayer(currentSource.stopsEndpoint, 'hvv-stops', 'stops');
        refreshLines();
        refreshStops();
        pollTimers.push(setInterval(refreshLines, REFERENCE_POLL_INTERVAL_MS));
        pollTimers.push(setInterval(refreshStops, REFERENCE_POLL_INTERVAL_MS));

        refreshHvvPositions();
        pollTimers.push(setInterval(refreshHvvPositions, HVV_POLL_INTERVAL_MS));

        if (currentSource.disruptionsEndpoint) {
            refreshDisruptions();
            pollTimers.push(setInterval(refreshDisruptions, DISRUPTIONS_POLL_INTERVAL_MS));
        }
    }

    // Swaps endpoints, resets timers/data, and hides disruption checkboxes for sources without them.
    function switchSource(sourceKey) {
        const next = SOURCES.find((s) => s.key === sourceKey);
        if (!next || next === currentSource) {
            return;
        }
        stopPolling();
        clearAllData();
        currentSource = next;
        modeToggleControl.setCategoriesVisible(!!currentSource.disruptionsEndpoint);
        modeToggleControl.setExtrasVisible(!!currentSource.straightToggle);
        startPolling();
    }

    function start() {
        setupSourcesAndLayers();
        startPolling();
    }

    if (map.isStyleLoaded()) {
        start();
    } else {
        map.once('load', start);
    }
}
