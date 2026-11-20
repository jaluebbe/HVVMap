// Optional AIS layer: HADAG ferries from /api/hvv/ais/hadag.geojson (see ais.py).
// Off by default; polls only while visible. Used by maplibre_map_hvv_base.js.

const AIS_ENDPOINT = '/api/hvv/ais/hadag.geojson';
const AIS_POLL_INTERVAL_MS = 30000;
const AIS_SOURCE = 'hvv-ais';
const AIS_LAYER = 'hvv-ais-vessels';
const AIS_PROJECTION_SOURCE = 'hvv-ais-projection';
const AIS_PROJECTION_LINE = 'hvv-ais-projection-line';
const AIS_PROJECTION_END = 'hvv-ais-projection-end';
const AIS_PROJECTION_UPDATE_MS = 5000;
// A projection is only drawn for moving vessels with a message this old.
const AIS_PROJECTION_MIN_AGE_S = 20;
const AIS_PROJECTION_MAX_AGE_S = 600;
const AIS_MOVING_KNOTS = 0.5;
const EARTH_RADIUS_M = 6371000;
const AIS_ATTRIBUTION = '<a href="https://openwaters.io/ais" target="_blank">AIS: Open Waters</a>';
const AIS_COLORS = { underway: '#00897b', moored: '#9aa5b1' };

// Top view of a hull, bow up (rotation 0 = north); drawn at 2x.
function buildAisIcon(color) {
    const scale = 2;
    const canvas = document.createElement('canvas');
    canvas.width = 18 * scale;
    canvas.height = 40 * scale;
    const ctx = canvas.getContext('2d');
    ctx.scale(scale, scale);
    ctx.beginPath();
    ctx.moveTo(9, 2);
    ctx.lineTo(15.5, 12);
    ctx.lineTo(15.5, 35);
    ctx.quadraticCurveTo(15.5, 38, 12.5, 38);
    ctx.lineTo(5.5, 38);
    ctx.quadraticCurveTo(2.5, 38, 2.5, 35);
    ctx.lineTo(2.5, 12);
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = '#ffffff';
    ctx.stroke();
    ctx.fillStyle = 'rgba(255, 255, 255, 0.85)';
    ctx.fillRect(6, 19, 6, 9);
    return ctx.getImageData(0, 0, canvas.width, canvas.height);
}

// Point reached from [lon, lat] after `meters` along the bearing `course` (great circle).
function destinationPoint([lon, lat], course, meters) {
    const rad = Math.PI / 180;
    const delta = meters / EARTH_RADIUS_M;
    const theta = course * rad;
    const phi1 = lat * rad;
    const phi2 = Math.asin(Math.sin(phi1) * Math.cos(delta) + Math.cos(phi1) * Math.sin(delta) * Math.cos(theta));
    const lambda2 =
        lon * rad +
        Math.atan2(Math.sin(theta) * Math.sin(delta) * Math.cos(phi1), Math.cos(delta) - Math.sin(phi1) * Math.sin(phi2));
    return [((lambda2 / rad + 540) % 360) - 180, phi2 / rad];
}

// Dead reckoning: a dashed line from the last reported position along the course
// to where the vessel would be now at the reported speed.
function buildProjections(collection, nowMs) {
    const features = [];
    (collection.features || []).forEach((feature) => {
        const p = feature.properties;
        if (p.state !== 'underway' || p.cog === null || p.cog === undefined || !p.seen) {
            return;
        }
        if (!(p.sog >= AIS_MOVING_KNOTS)) {
            return;
        }
        const ageS = (nowMs - Date.parse(p.seen)) / 1000;
        if (!(ageS >= AIS_PROJECTION_MIN_AGE_S && ageS <= AIS_PROJECTION_MAX_AGE_S)) {
            return;
        }
        const start = feature.geometry.coordinates;
        const end = destinationPoint(start, p.cog, p.sog * 0.514444 * ageS);
        // older messages are less certain, so the indicator fades out
        const opacity = 0.7 - 0.45 * (ageS / AIS_PROJECTION_MAX_AGE_S);
        features.push(
            { type: 'Feature', properties: { opacity }, geometry: { type: 'LineString', coordinates: [start, end] } },
            { type: 'Feature', properties: { opacity }, geometry: { type: 'Point', coordinates: end } },
        );
    });
    return { type: 'FeatureCollection', features };
}

function aisPopupHtml(p) {
    const knots = p.sog === null || p.sog === undefined ? '' : `${Number(p.sog).toFixed(1)} kn`;
    const age = p.seen ? Math.max(0, Math.round((Date.now() - Date.parse(p.seen)) / 60000)) : null;
    const lines = [
        `<b>${p.name}</b>`,
        [p.state === 'underway' ? 'in Fahrt' : 'festgemacht', knots].filter(Boolean).join(', '),
        age === null ? '' : `Position vor ${age} min`,
    ];
    return lines.filter(Boolean).join('<br>');
}

function createHvvAisLayer(map) {
    const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, anchor: 'bottom', offset: 14 });
    let timer = null;
    let projectionTimer = null;
    let visible = false;
    let lastData = { type: 'FeatureCollection', features: [] };

    Object.entries(AIS_COLORS).forEach(([state, color]) => {
        map.addImage(`hvv-ais-${state}`, buildAisIcon(color), { pixelRatio: 2 });
    });
    // The source attribution shows up in the attribution control while the layer is visible.
    map.addSource(AIS_SOURCE, {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
        attribution: AIS_ATTRIBUTION,
    });
    map.addSource(AIS_PROJECTION_SOURCE, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
    // drawn below the vessel icons
    map.addLayer({
        id: AIS_PROJECTION_LINE,
        type: 'line',
        source: AIS_PROJECTION_SOURCE,
        filter: ['==', ['geometry-type'], 'LineString'],
        layout: { visibility: 'none', 'line-cap': 'round' },
        paint: {
            'line-color': AIS_COLORS.underway,
            'line-width': 1.2,
            'line-dasharray': [2, 3],
            'line-opacity': ['get', 'opacity'],
        },
    });
    map.addLayer({
        id: AIS_PROJECTION_END,
        type: 'circle',
        source: AIS_PROJECTION_SOURCE,
        filter: ['==', ['geometry-type'], 'Point'],
        layout: { visibility: 'none' },
        paint: {
            'circle-radius': 3,
            'circle-color': '#ffffff',
            'circle-opacity': ['get', 'opacity'],
            'circle-stroke-color': AIS_COLORS.underway,
            'circle-stroke-width': 1.2,
            'circle-stroke-opacity': ['get', 'opacity'],
        },
    });
    map.addLayer({
        id: AIS_LAYER,
        type: 'symbol',
        source: AIS_SOURCE,
        layout: {
            visibility: 'none',
            'icon-image': ['concat', 'hvv-ais-', ['get', 'state']],
            'icon-rotate': ['get', 'rotation'],
            'icon-rotation-alignment': 'map',
            'icon-allow-overlap': true,
            'icon-ignore-placement': true,
            'icon-size': ['interpolate', ['linear'], ['zoom'], 9, 0.45, 13, 0.8, 17, 1.4],
            // vessels in motion are drawn above those lying still
            'symbol-sort-key': ['case', ['==', ['get', 'state'], 'underway'], 1, 0],
            'text-field': ['step', ['zoom'], '', 12, ['get', 'name']],
            'text-font': ['KlokanTech Noto Sans Regular'],
            'text-size': 10,
            'text-offset': [0, 1.8],
            'text-anchor': 'top',
            'text-optional': true,
        },
        paint: {
            'icon-opacity': ['case', ['==', ['get', 'state'], 'underway'], 1, 0.8],
            'text-color': '#37474f',
            'text-halo-color': 'rgba(255, 255, 255, 0.9)',
            'text-halo-width': 1.2,
        },
    });

    function show(event) {
        map.getCanvas().style.cursor = 'pointer';
        const feature = event.features[0];
        popup.setLngLat(feature.geometry.coordinates).setHTML(aisPopupHtml(feature.properties)).addTo(map);
    }
    map.on('mouseenter', AIS_LAYER, show);
    map.on('click', AIS_LAYER, show);
    map.on('mouseleave', AIS_LAYER, () => {
        map.getCanvas().style.cursor = '';
        popup.remove();
    });

    function updateProjections() {
        map.getSource(AIS_PROJECTION_SOURCE).setData(buildProjections(lastData, Date.now()));
    }

    async function refresh() {
        try {
            const response = await fetch(AIS_ENDPOINT);
            if (response.ok) {
                lastData = await response.json();
                map.getSource(AIS_SOURCE).setData(lastData);
                updateProjections();
            }
        } catch (error) {
            // keep the vessels of the last successful poll
        }
    }

    return {
        setVisible(show) {
            visible = show;
            [AIS_LAYER, AIS_PROJECTION_LINE, AIS_PROJECTION_END].forEach((id) => {
                map.setLayoutProperty(id, 'visibility', show ? 'visible' : 'none');
            });
            clearInterval(timer);
            clearInterval(projectionTimer);
            timer = projectionTimer = null;
            if (show) {
                refresh();
                timer = setInterval(refresh, AIS_POLL_INTERVAL_MS);
                projectionTimer = setInterval(updateProjections, AIS_PROJECTION_UPDATE_MS);
            } else {
                popup.remove();
            }
        },
        get visible() {
            return visible;
        },
    };
}
