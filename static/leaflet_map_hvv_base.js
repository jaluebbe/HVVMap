/* Minimal base map for the HVV live vehicle page: one vector basemap layer
 * (Positron), attribution incl. privacy link, centered on Hamburg. Separate
 * from leaflet_map_offline.js on purpose - that one is also used by the
 * editor and carries features this page doesn't need.
 */

const minZoom = 7;
const maxZoom = 18;
const map = L.map('map', {
    minZoom: minZoom,
    maxZoom: maxZoom,
});

map.attributionControl.addAttribution('<a href="https://github.com/jaluebbe/HVVMap">Source on GitHub</a>');
map.attributionControl.addAttribution('<a href="https://www.hvv.de/" target="_blank">Fahrplandaten: Hamburger Verkehrsverbund GmbH</a>');

function addPrivacyStatement() {
    var xhr = new XMLHttpRequest();
    xhr.open('HEAD', "/static/datenschutz.html");
    xhr.onload = function() {
        if (xhr.status === 200)
            map.attributionControl.addAttribution(
                '<a href="/static/datenschutz.html" target="_blank">Impressum & Datenschutzerkl&auml;rung</a>'
            );
    }
    xhr.send();
}
addPrivacyStatement();

const vectorBaseLayers = {}; // label -> L.maplibreGL instance

function addOSMVectorLayer(styleName, layerLabel, registerInControl = true) {
    let myLayer = L.maplibreGL({
        style: '/api/vector/style/' + styleName + '.json',
        attribution: '&copy; <a href="https://openmaptiles.org/">OpenMapTiles</a>, &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    });
    vectorBaseLayers[layerLabel] = myLayer;
    if (registerInControl) {
        layerControl.addBaseLayer(myLayer, layerLabel);
    }
    return myLayer;
}

L.control.scale({ 'imperial': false }).addTo(map);
const layerControl = L.control.layers({}, {}, {
    collapsed: L.Browser.mobile,
    position: 'topright',
}).addTo(map);

const HAMBURG_CENTER = [53.5511, 9.9937];
const HAMBURG_DEFAULT_ZOOM = 12;

// Single fixed basemap, no region selection needed - see api.py.
addOSMVectorLayer("positron", "OSM Positron", false).addTo(map);
map.setView(HAMBURG_CENTER, HAMBURG_DEFAULT_ZOOM);

function setupAttribution() {
    var attrControl = document.querySelector('.leaflet-control-attribution');
    if (!attrControl || document.getElementById('attr-toggle')) return;
    var attrContent = attrControl.innerHTML;
    attrControl.style.display = 'flex';
    attrControl.style.alignItems = 'center';
    attrControl.innerHTML =
        '<span id="attr-toggle" style="cursor:pointer;padding-right:2px;flex-shrink:0">ℹ️</span>' +
        '<span id="attr-content" style="display:none">' + attrContent + '</span>';
    document.getElementById('attr-toggle').addEventListener('click', function() {
        var content = document.getElementById('attr-content');
        content.style.display = content.style.display === 'none' ? 'inline' : 'none';
    });
}

map.whenReady(function() {
    setupAttribution();
    var observer = new MutationObserver(function() {
        if (!document.getElementById('attr-toggle')) {
            setupAttribution();
        }
    });
    observer.observe(document.querySelector('.leaflet-control-attribution'), {
        childList: true,
        subtree: true,
        characterData: true,
    });
});

function fixIOSResize() {
    setTimeout(() => {
        map.invalidateSize({ animate: false });
    }, 250);
}
window.addEventListener('orientationchange', fixIOSResize);
window.addEventListener('resize', fixIOSResize);

/* ── Shared HVV vehicle/line/stop/disruption layers ───────────────────────
 * Used by the live, realtime and GTFS pages, which differ only in their
 * endpoint URLs (and whether disruptions are supported at all) - passed in
 * via the config object given to initHvvVehicleLayers(). Matching styling
 * lives in /static/leaflet_map_hvv.css, linked from each page.
 */
const HVV_POLL_INTERVAL_MS = 1_000;
const REFERENCE_POLL_INTERVAL_MS = 60_000;
const DISRUPTIONS_POLL_INTERVAL_MS = 60_000;

// Positions, lines and stops are grouped per mode into one layerGroup, so
// one control entry toggles all three together.
const MODES = [
    { key: 'U', name: 'U-Bahn', icon: '/static/hvv/icons/u.svg' },
    { key: 'S', name: 'S-Bahn', icon: '/static/hvv/icons/s.svg' },
    { key: 'AKN', name: 'AKN', icon: '/static/hvv/icons/akn.svg' },
    { key: 'FERRY', name: 'Fähre', icon: '/static/hvv/icons/ferry.svg' },
];

// Vehicle model isn't its own property - parsed from properties.text (DT5/Batteriegelenkbus only).
const VEHICLE_MODEL_CLASSES = {
    'Batteriegelenkbus': 'hvv-vehicle-batteriegelenkbus',
    'DT5': 'hvv-vehicle-dt5',
};

function vehicleModelFromText(text, delay) {
    if (!text) {
        return '';
    }
    const parts = text.split('<br>');
    const modelParts = delay > 0 ? parts.slice(1, -1) : parts.slice(1);
    return modelParts.join('');
}

function hvvPointToLayer(feature, latlng) {
    const properties = feature.properties || {};
    if (properties.icon) {
        const height = properties.iconHeight || 20;
        // Space between icon and label scales with line name length.
        const lineNameLength = (properties.line || '').length || 2;
        const labelOffset = Math.round(height * (0.28*lineNameLength+0.9));
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
        return L.marker(latlng, {
            interactive: true,
            pane: 'hvvTrainsPane',
            icon: L.divIcon({
                className: 'geojson-svg-icon',
                html: `<div class="hvv-marker-wrapper"><img src="${properties.icon}" style="height:${height}px" />${label}${delayStandalone}</div>`,
                iconSize: null,
                iconAnchor: [0, 0],
            }),
        });
    }
    const circleOptions = Object.assign({}, properties, { pane: 'hvvTrainsPane' });
    if ('markerRadius' in properties) {
        circleOptions.radius = properties.markerRadius;
    }
    return L.circleMarker(latlng, circleOptions);
}

function hvvStyle(feature) {
    const properties = feature.properties || {};
    const style = {};
    if (properties.color) {
        style.color = properties.color;
    }
    return style;
}

function hvvOnEachFeature(feature, layer) {
    const properties = feature.properties || {};
    if (properties.text) {
        layer.bindTooltip(properties.text, {
            sticky: true,
            direction: 'top',
            offset: [0, -5],
        });
    }
}

// Only two of three categories shown - SONSTIGE stays too vague for a
// useful map display.
const DISRUPTION_CATEGORIES = [
    { key: 'SPERRUNG', name: 'Sperrung', icon: '🚧', visible: true },
    { key: 'BARRIEREFREIHEIT', name: 'Aufzüge', icon: '♿', visible: false },
];

function hvvDisruptionPointToLayer(feature, latlng) {
    const properties = feature.properties || {};
    return L.circleMarker(latlng, {
        pane: 'hvvDisruptionsPane',
        radius: properties.markerRadius || 5,
        fillColor: properties.color || '#888888',
        fillOpacity: 1,
        color: properties.color || '#888888',
        weight: 1,
    });
}

function hvvDisruptionOnEachFeature(feature, layer) {
    const properties = feature.properties || {};
    if (properties.text) {
        layer.bindTooltip(properties.text, {
            sticky: true,
            direction: 'top',
            offset: [0, -5],
        });
    }
}

// config: { positionsEndpoint, linesEndpoint, stopsEndpoint, disruptionsEndpoint }
// disruptionsEndpoint is optional - omit it for backends that don't support
// disruptions (e.g. GTFS), which then skips that layer entirely.
function initHvvVehicleLayers(config) {
    // Explicit panes for a deterministic stacking order: lines/stops load
    // asynchronously via fetch(), so their arrival order isn't guaranteed.
    // Order: lines < stops < trains (default markerPane is 600).
    map.createPane('hvvLinesPane');
    map.getPane('hvvLinesPane').style.zIndex = 410;
    map.createPane('hvvStopsPane');
    map.getPane('hvvStopsPane').style.zIndex = 420;
    if (config.disruptionsEndpoint) {
        map.createPane('hvvDisruptionsPane');
        map.getPane('hvvDisruptionsPane').style.zIndex = 430;
    }
    map.createPane('hvvTrainsPane');
    map.getPane('hvvTrainsPane').style.zIndex = 620;

    const modeLayers = {};
    const groupToMode = new Map();
    const activeModes = new Set();

    MODES.forEach(function(m) {
        const positions = L.geoJSON([], {
            pointToLayer: hvvPointToLayer,
            style: hvvStyle,
            onEachFeature: hvvOnEachFeature,
        });
        const lines = L.geoJSON([], {
            style: function(feature) {
                const properties = feature.properties || {};
                return {
                    pane: 'hvvLinesPane',
                    color: properties.color || '#888888',
                    weight: 1.5,
                    opacity: 0.85,
                    dashArray: '2,6',
                };
            },
        });
        const stops = L.geoJSON([], {
            pointToLayer: function(feature, latlng) {
                return L.circleMarker(latlng, {
                    pane: 'hvvStopsPane',
                    radius: 4,
                    fillColor: '#ffffff',
                    fillOpacity: 1,
                    color: '#000000',
                    weight: 1,
                });
            },
            onEachFeature: function(feature, layer) {
                const properties = feature.properties || {};
                if (properties.text) {
                    layer.bindTooltip(properties.text, {
                        sticky: true,
                        direction: 'top',
                        offset: [0, -5],
                    });
                }
            },
        });

        const group = L.layerGroup([lines, stops, positions]);
        group.addTo(map);
        const labelHtml = `<img src="${m.icon}" height="16" style="vertical-align:middle;margin-right:4px;">${m.name}`;
        layerControl.addOverlay(group, labelHtml);
        modeLayers[m.key] = { positions, lines, stops, group };
        groupToMode.set(group, m.key);
        activeModes.add(m.key);
    });

    // Reference layers (lines/stops) may change over time, hence periodic reloads.
    async function refreshReferenceLayer(endpoint, layerKey, matchesMode) {
        try {
            const response = await fetch(endpoint, { cache: 'no-store' });
            if (!response.ok) {
                console.warn(`HVV ${layerKey} layer: HTTP`, response.status);
                return;
            }
            const data = await response.json();
            MODES.forEach(function(m) {
                const filtered = {
                    type: 'FeatureCollection',
                    features: data.features.filter(f => matchesMode(f, m.key)),
                };
                modeLayers[m.key][layerKey].clearLayers();
                modeLayers[m.key][layerKey].addData(filtered);
            });
        } catch (error) {
            console.warn(`Failed to load HVV ${layerKey} layer:`, error);
        }
    }

    function refreshReferenceLines() {
        return refreshReferenceLayer(
            config.linesEndpoint,
            'lines',
            (f, mode) => (f.properties?.modes || []).includes(mode),
        );
    }

    function refreshReferenceStops() {
        return refreshReferenceLayer(
            config.stopsEndpoint,
            'stops',
            (f, mode) => (f.properties?.modes || []).includes(mode),
        );
    }

    refreshReferenceLines();
    refreshReferenceStops();
    setInterval(refreshReferenceLines, REFERENCE_POLL_INTERVAL_MS);
    setInterval(refreshReferenceStops, REFERENCE_POLL_INTERVAL_MS);

    async function refreshHvvPositions() {
        try {
            const response = await fetch(config.positionsEndpoint, { cache: 'no-store' });
            if (!response.ok) {
                console.warn('HVV positions: HTTP', response.status);
                return;
            }
            const data = await response.json();
            MODES.forEach(function(m) {
                const filtered = {
                    type: 'FeatureCollection',
                    features: data.features.filter(f => f.properties.mode === m.key),
                };
                modeLayers[m.key].positions.clearLayers();
                modeLayers[m.key].positions.addData(filtered);
            });
        } catch (error) {
            console.warn('Failed to load HVV positions:', error);
        }
    }

    refreshHvvPositions();
    setInterval(refreshHvvPositions, HVV_POLL_INTERVAL_MS);

    map.zoomControl.setPosition('topleft');

    if (config.disruptionsEndpoint) {
        const disruptionLayers = {};
        let disruptionLayersRegistered = false;
        DISRUPTION_CATEGORIES.forEach(function(c) {
            const layer = L.geoJSON([], {
                pointToLayer: hvvDisruptionPointToLayer,
                onEachFeature: hvvDisruptionOnEachFeature,
            });
            if (c.visible) {
                layer.addTo(map);
            }
            disruptionLayers[c.key] = layer;
        });

        // Only added to the layer control once the first fetch actually
        // succeeds, so a checkbox never appears for data that isn't there.
        function registerDisruptionLayersInControl() {
            if (disruptionLayersRegistered) {
                return;
            }
            DISRUPTION_CATEGORIES.forEach(function(c) {
                layerControl.addOverlay(disruptionLayers[c.key], `${c.icon} ${c.name}`);
            });
            disruptionLayersRegistered = true;
        }

        // Kept for re-filtering on mode toggle without re-fetching.
        let disruptionRawData = null;

        function renderDisruptionLayers() {
            if (!disruptionRawData) {
                return;
            }
            DISRUPTION_CATEGORIES.forEach(function(c) {
                const filtered = {
                    type: 'FeatureCollection',
                    // Show only if at least one of its modes is active (an
                    // empty modes array is never hidden, as a safe default).
                    features: disruptionRawData.features.filter(function(f) {
                        if (f.properties?.category !== c.key) {
                            return false;
                        }
                        const modes = f.properties?.modes || [];
                        return modes.length === 0 || modes.some(mode => activeModes.has(mode));
                    }),
                };
                disruptionLayers[c.key].clearLayers();
                disruptionLayers[c.key].addData(filtered);
            });
        }

        // Updated by hvvmap-fetcher roughly every 10 minutes, hence polling.
        async function refreshDisruptions() {
            try {
                const response = await fetch(config.disruptionsEndpoint, { cache: 'no-store' });
                if (!response.ok) {
                    console.warn('HVV disruptions: HTTP', response.status);
                    return;
                }
                disruptionRawData = await response.json();
                renderDisruptionLayers();
                registerDisruptionLayersInControl();
            } catch (error) {
                console.warn('Failed to load HVV disruptions:', error);
            }
        }

        refreshDisruptions();
        setInterval(refreshDisruptions, DISRUPTIONS_POLL_INTERVAL_MS);

        // Update disruptions when a vehicle layer is toggled.
        map.on('overlayadd', function(e) {
            const modeKey = groupToMode.get(e.layer);
            if (modeKey) {
                activeModes.add(modeKey);
                renderDisruptionLayers();
            }
        });
        map.on('overlayremove', function(e) {
            const modeKey = groupToMode.get(e.layer);
            if (modeKey) {
                activeModes.delete(modeKey);
                renderDisruptionLayers();
            }
        });
    }

    // Toggle visibility of vehicle destination labels.
    const LabelToggleControl = L.Control.extend({
        options: { position: 'topright' },
        onAdd: function() {
            const container = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
            const button = L.DomUtil.create('a', '', container);
            button.href = '#';
            button.title = 'Ziel-Labels ein-/ausblenden';
            button.style.display = 'flex';
            button.style.alignItems = 'center';
            button.style.justifyContent = 'center';
            button.style.fontSize = '16px';
            button.innerHTML = '🏷️';
            L.DomEvent.disableClickPropagation(container);
            L.DomEvent.on(button, 'click', function(e) {
                L.DomEvent.preventDefault(e);
                map.getContainer().classList.toggle('hvv-labels-hidden');
            });
            return container;
        },
    });
    map.addControl(new LabelToggleControl());
}
