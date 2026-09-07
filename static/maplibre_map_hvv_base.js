/* Minimal MapLibre GL JS base map for the HVV live vehicle page - a
 * parallel, independent structure to leaflet_map_hvv_base.js, so future
 * work isn't tied to Leaflet if that's ever needed. Live page only for now;
 * everything the Leaflet page's layer control offers (U/S/AKN/Fähre modes,
 * Sperrung/Aufzüge disruption categories) is ported (see initHvvVehicleLayers).
 */

const HAMBURG_CENTER = [9.9937, 53.5511]; // MapLibre wants [lon, lat]
const HAMBURG_DEFAULT_ZOOM = 12;

const map = new maplibregl.Map({
    container: 'map',
    style: '/api/vector/style/positron.json',
    center: HAMBURG_CENTER,
    zoom: HAMBURG_DEFAULT_ZOOM,
    minZoom: 9,
    maxZoom: 18,
    attributionControl: false, // added manually below, with our own extra links
});

map.addControl(new maplibregl.NavigationControl(), 'top-left');
map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');

// compact:true gives the same collapsible "i" toggle the Leaflet page builds
// by hand (setupAttribution()) - built into MapLibre, no custom JS needed.
const attributionControl = new maplibregl.AttributionControl({
    compact: true,
    customAttribution: [
        '<a href="https://github.com/jaluebbe/HVVMap">Source on GitHub</a>',
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
        const container = document.createElement('div');
        container.className = 'maplibregl-ctrl maplibregl-ctrl-group';
        const button = document.createElement('button');
        button.type = 'button';
        button.title = 'Ziel-Labels ein-/ausblenden';
        button.style.fontSize = '16px';
        button.textContent = '🏷️';
        button.addEventListener('click', function() {
            mapInstance.getContainer().classList.toggle('hvv-labels-hidden');
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
// Added to the map later, after the mode selector (see setupSourcesAndLayers)
// so it stacks below it in the top-right corner, matching the Leaflet page.

const HVV_POLL_INTERVAL_MS = 1_000;
const REFERENCE_POLL_INTERVAL_MS = 60_000;
const DISRUPTIONS_POLL_INTERVAL_MS = 60_000;

const EMPTY_FEATURE_COLLECTION = { type: 'FeatureCollection', features: [] };

// Positions, lines and stops are grouped per mode; ModeToggleControl below
// toggles all three together for a given mode, mirroring the Leaflet page's
// per-mode layerGroup + L.control.layers checkboxes.
const MODES = [
    { key: 'U', name: 'U-Bahn', icon: '/static/hvv/icons/u.svg' },
    { key: 'S', name: 'S-Bahn', icon: '/static/hvv/icons/s.svg' },
    { key: 'AKN', name: 'AKN', icon: '/static/hvv/icons/akn.svg' },
    { key: 'FERRY', name: 'Fähre', icon: '/static/hvv/icons/ferry.svg' },
];

// Only two of three categories shown - SONSTIGE stays too vague for a
// useful map display (matches the Leaflet page).
const DISRUPTION_CATEGORIES = [
    { key: 'SPERRUNG', name: 'Sperrung', icon: '🚧', visible: true },
    { key: 'BARRIEREFREIHEIT', name: 'Aufzüge', icon: '♿', visible: false },
];

function buildIconNode(icon) {
    if (icon.startsWith('/')) {
        const img = document.createElement('img');
        img.src = icon;
        img.height = 16;
        return img;
    }
    const span = document.createElement('span');
    span.textContent = icon;
    return span;
}

// One checkbox per mode plus (if given) one per disruption category, in a
// single panel - onModeToggle(modeKey, visible) / onCategoryToggle(categoryKey,
// visible) are wired up by initHvvVehicleLayers() to the actual layers/markers.
class ModeToggleControl {
    constructor(onModeToggle, categories, onCategoryToggle) {
        this._onModeToggle = onModeToggle;
        this._categories = categories || [];
        this._onCategoryToggle = onCategoryToggle;
    }

    _appendItem(container, key, name, icon, checked, onToggle) {
        const label = document.createElement('label');
        label.className = 'hvv-mode-control-item';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = checked;
        checkbox.addEventListener('change', () => onToggle(key, checkbox.checked));
        label.append(checkbox, buildIconNode(icon), document.createTextNode(name));
        container.appendChild(label);
    }

    onAdd(mapInstance) {
        this._map = mapInstance;
        const container = document.createElement('div');
        container.className = 'maplibregl-ctrl maplibregl-ctrl-group hvv-mode-control';
        MODES.forEach((m) => this._appendItem(container, m.key, m.name, m.icon, true, this._onModeToggle));
        if (this._categories.length > 0) {
            const divider = document.createElement('div');
            divider.className = 'hvv-mode-control-divider';
            container.appendChild(divider);
            this._categories.forEach((c) => {
                this._appendItem(container, c.key, c.name, c.icon, c.visible, this._onCategoryToggle);
            });
        }
        this._container = container;
        return container;
    }

    onRemove() {
        this._container.parentNode.removeChild(this._container);
        this._map = undefined;
    }
}

// Builds one marker's DOM element from its GeoJSON properties - a close
// port of leaflet_map_hvv_base.js's hvvPointToLayer(), minus the Leaflet
// divIcon wrapping. A separate .hvv-marker-hitbox child gives it a real,
// generously-sized hoverable area (not just whatever the icon/label happen
// to occupy) - a sibling, not a change to the wrapper itself, since MapLibre
// applies its own positioning transform ON TOP of the wrapper's box: shifting
// the wrapper's own left/top would shift the whole marker, not just widen
// its hit area.
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
        const label = properties.label
            ? `<span class="hvv-marker-label" style="left:${labelOffset}px;">${properties.label}${delayBadge}</span>`
            : '';
        // Stays visible when labels are hidden overall (see CSS).
        const delayStandalone = delayText
            ? `<span class="hvv-marker-delay-standalone" style="left:${labelOffset}px;">${delayText}</span>`
            : '';
        wrapper.innerHTML = `<img src="${properties.icon}" style="height:${height}px" />${label}${delayStandalone}`;

        // Hit area reaching from the icon's left edge out past the label's
        // (roughly estimated) right edge - doesn't need to be pixel-exact,
        // just comfortable to hit. Positioned relative to the same origin
        // as the icon/label (wrapper's local (0,0) = the map point).
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

    // Fallback for lines without a known icon key - a plain colored dot,
    // matching the live API path's L.circleMarker fallback. Self-centers on
    // the wrapper's origin the same way the icon <img> above does.
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


// config: { positionsEndpoint, linesEndpoint, stopsEndpoint, disruptionsEndpoint }
// A full port of the Leaflet page's layer control - U/S/AKN/Fähre modes and,
// if disruptionsEndpoint is given, the Sperrung/Aufzüge disruption categories.
function initHvvVehicleLayers(config) {
    // Shared by every hover handler below (vehicles, stops, disruptions) so
    // there is only ever one popup in existence - two features close
    // together can never show overlapping popups at the same time.
    // anchor:'bottom' pins the popup above the point with its tip pointing
    // straight down at it, instead of MapLibre's automatic corner choice.
    const hoverPopup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, anchor: 'bottom' });
    const activeModes = new Set(MODES.map((m) => m.key));
    const positionMarkersByMode = {};
    MODES.forEach((m) => { positionMarkersByMode[m.key] = []; });

    function setModeVisibility(modeKey, visible) {
        if (visible) {
            activeModes.add(modeKey);
        } else {
            activeModes.delete(modeKey);
        }
        const visibility = visible ? 'visible' : 'none';
        map.setLayoutProperty(`hvv-lines-layer-${modeKey}`, 'visibility', visibility);
        map.setLayoutProperty(`hvv-stops-layer-${modeKey}`, 'visibility', visibility);
        positionMarkersByMode[modeKey].forEach((marker) => {
            marker.getElement().style.display = visible ? '' : 'none';
        });
        renderDisruptionLayers();
    }

    function setCategoryVisibility(categoryKey, visible) {
        map.setLayoutProperty(`hvv-disruptions-layer-${categoryKey}`, 'visibility', visible ? 'visible' : 'none');
    }

    function setupSourcesAndLayers() {
        const hoverLayers = [];
        MODES.forEach(function(m) {
            map.addSource(`hvv-lines-${m.key}`, { type: 'geojson', data: EMPTY_FEATURE_COLLECTION });
            map.addLayer({
                id: `hvv-lines-layer-${m.key}`,
                type: 'line',
                source: `hvv-lines-${m.key}`,
                paint: {
                    'line-color': ['coalesce', ['get', 'color'], '#888888'],
                    'line-width': 1.5,
                    'line-opacity': 0.85,
                    'line-dasharray': [1, 2],
                },
            });

            map.addSource(`hvv-stops-${m.key}`, { type: 'geojson', data: EMPTY_FEATURE_COLLECTION });
            map.addLayer({
                id: `hvv-stops-layer-${m.key}`,
                type: 'circle',
                source: `hvv-stops-${m.key}`,
                paint: {
                    'circle-radius': 4,
                    'circle-color': '#ffffff',
                    'circle-stroke-color': '#000000',
                    'circle-stroke-width': 1,
                },
            });
            hoverLayers.push(`hvv-stops-layer-${m.key}`);
        });

        if (config.disruptionsEndpoint) {
            DISRUPTION_CATEGORIES.forEach(function(c) {
                map.addSource(`hvv-disruptions-${c.key}`, { type: 'geojson', data: EMPTY_FEATURE_COLLECTION });
                map.addLayer({
                    id: `hvv-disruptions-layer-${c.key}`,
                    type: 'circle',
                    source: `hvv-disruptions-${c.key}`,
                    layout: { visibility: c.visible ? 'visible' : 'none' },
                    paint: {
                        'circle-radius': ['coalesce', ['get', 'markerRadius'], 5],
                        'circle-color': ['coalesce', ['get', 'color'], '#888888'],
                    },
                });
                hoverLayers.push(`hvv-disruptions-layer-${c.key}`);
            });
        }

        // Lightweight hover popup - GL circle layers have no Leaflet-style
        // bindTooltip() equivalent, this is close enough for a test page.
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

        map.addControl(
            new ModeToggleControl(
                setModeVisibility,
                config.disruptionsEndpoint ? DISRUPTION_CATEGORIES : [],
                setCategoryVisibility,
            ),
            'top-right',
        );
        map.addControl(new LabelToggleControl(), 'top-right');
    }

    async function refreshReferenceLayer(endpoint, sourcePrefix, layerName) {
        try {
            const response = await fetch(endpoint, { cache: 'no-store' });
            if (!response.ok) {
                console.warn(`HVV ${layerName} layer: HTTP`, response.status);
                return;
            }
            const data = await response.json();
            MODES.forEach(function(m) {
                const filtered = {
                    type: 'FeatureCollection',
                    features: data.features.filter((f) => (f.properties?.modes || []).includes(m.key)),
                };
                map.getSource(`${sourcePrefix}-${m.key}`).setData(filtered);
            });
        } catch (error) {
            console.warn(`Failed to load HVV ${layerName} layer:`, error);
        }
    }

    async function refreshHvvPositions() {
        try {
            const response = await fetch(config.positionsEndpoint, { cache: 'no-store' });
            if (!response.ok) {
                console.warn('HVV positions: HTTP', response.status);
                return;
            }
            const data = await response.json();
            // Cleared and recreated every poll rather than diffed/reused -
            // simple on purpose for this first test; fine at HVV's vehicle
            // counts, worth revisiting if flicker becomes noticeable. No
            // explicit hoverPopup.remove() here: removing a hovered marker's
            // element already fires its mouseleave, closing the popup on its
            // own - force-closing it unconditionally every second would also
            // kill an unrelated, still-hovered stop/disruption popup.
            MODES.forEach((m) => {
                positionMarkersByMode[m.key].forEach((marker) => marker.remove());
                positionMarkersByMode[m.key] = [];
            });
            data.features.forEach(function(feature) {
                const properties = feature.properties || {};
                const modeKey = properties.mode;
                if (!positionMarkersByMode[modeKey]) {
                    return; // unknown mode - shouldn't happen, but don't crash the poll
                }
                const element = buildMarkerElement(properties);
                // anchor:'top-left' - the wrapper's own origin is the point
                // itself, matching Leaflet's iconAnchor:[0,0]; the icon/dot
                // then re-centers itself via CSS transform (see buildMarkerElement).
                const marker = new maplibregl.Marker({ element, anchor: 'top-left' })
                    .setLngLat(feature.geometry.coordinates)
                    .addTo(map);
                if (!activeModes.has(modeKey)) {
                    element.style.display = 'none';
                }
                if (properties.text) {
                    // A real DOM element, so hover can be wired up directly -
                    // no need for the map's layer-based mouseenter/mouseleave
                    // used for the GL stops/disruptions layers below.
                    element.addEventListener('mouseenter', function() {
                        // Extra gap above the icon (anchor:'bottom' already
                        // points the tip straight down at it) so the popup
                        // doesn't sit flush against the icon itself.
                        const halfIconHeight = (properties.iconHeight || 20) / 2;
                        hoverPopup
                            .setOffset(halfIconHeight)
                            .setLngLat(feature.geometry.coordinates)
                            .setHTML(properties.text)
                            .addTo(map);
                    });
                    element.addEventListener('mouseleave', function() {
                        hoverPopup.remove();
                    });
                }
                positionMarkersByMode[modeKey].push(marker);
            });
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
                // Data is kept in sync for every category regardless of its
                // current visibility, so toggling one on shows up immediately
                // instead of waiting for the next poll.
                features: disruptionRawData.features.filter(function(f) {
                    if (f.properties?.category !== c.key) {
                        return false;
                    }
                    const modes = f.properties?.modes || [];
                    return modes.length === 0 || modes.some((mode) => activeModes.has(mode));
                }),
            };
            source.setData(filtered);
        });
    }

    async function refreshDisruptions() {
        try {
            const response = await fetch(config.disruptionsEndpoint, { cache: 'no-store' });
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

    function start() {
        setupSourcesAndLayers();

        const refreshLines = () => refreshReferenceLayer(config.linesEndpoint, 'hvv-lines', 'lines');
        const refreshStops = () => refreshReferenceLayer(config.stopsEndpoint, 'hvv-stops', 'stops');
        refreshLines();
        refreshStops();
        setInterval(refreshLines, REFERENCE_POLL_INTERVAL_MS);
        setInterval(refreshStops, REFERENCE_POLL_INTERVAL_MS);

        refreshHvvPositions();
        setInterval(refreshHvvPositions, HVV_POLL_INTERVAL_MS);

        if (config.disruptionsEndpoint) {
            refreshDisruptions();
            setInterval(refreshDisruptions, DISRUPTIONS_POLL_INTERVAL_MS);
        }
    }

    if (map.isStyleLoaded()) {
        start();
    } else {
        map.once('load', start);
    }
}
