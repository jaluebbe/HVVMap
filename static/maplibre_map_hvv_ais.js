// Optional AIS layers: HADAG ferries from /api/hvv/ais/*.geojson (ais.py,
// aisstream.py). Off by default; poll only while visible. Used by
// maplibre_map_hvv_base.js.

const AIS_SOURCES = {
    ais: {
        endpoint: '/api/hvv/ais/hadag.geojson',
        pollMs: 30000,
        label: 'Open Waters',
        attribution: '<a href="https://openwaters.io/ais" target="_blank">AIS: Open Waters</a>',
        colors: { underway: '#00897b', moored: '#9aa5b1' },
    },
    aisstream: {
        endpoint: '/api/hvv/ais/aisstream.geojson',
        pollMs: 15000,
        label: 'aisstream.io',
        attribution: '<a href="https://aisstream.io" target="_blank">AIS: aisstream.io</a>',
        colors: { underway: '#6a1b9a', moored: '#b39ddb' },
    },
};

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

function aisPopupHtml(p, label) {
    const knots = p.sog === null || p.sog === undefined ? '' : `${Number(p.sog).toFixed(1)} kn`;
    const age = p.seen ? Math.max(0, Math.round((Date.now() - Date.parse(p.seen)) / 60000)) : null;
    const lines = [
        `<b>${p.name}</b>`,
        [p.state === 'underway' ? 'in Fahrt' : 'festgemacht', knots].filter(Boolean).join(', '),
        age === null ? '' : `Position vor ${age} min`,
        `<span style="color:#78909c">${label}</span>`,
    ];
    return lines.filter(Boolean).join('<br>');
}

function createHvvAisLayer(map, key = 'ais') {
    const config = AIS_SOURCES[key];
    const source = `hvv-${key}`;
    const layer = `hvv-${key}-vessels`;
    const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, anchor: 'bottom', offset: 14 });
    let timer = null;
    let visible = false;

    Object.entries(config.colors).forEach(([state, color]) => {
        map.addImage(`${source}-${state}`, buildAisIcon(color), { pixelRatio: 2 });
    });
    // The source attribution shows up in the attribution control while the layer is visible.
    map.addSource(source, {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
        attribution: config.attribution,
    });
    map.addLayer({
        id: layer,
        type: 'symbol',
        source,
        layout: {
            visibility: 'none',
            'icon-image': ['concat', `${source}-`, ['get', 'state']],
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
        popup.setLngLat(feature.geometry.coordinates).setHTML(aisPopupHtml(feature.properties, config.label)).addTo(map);
    }
    map.on('mouseenter', layer, show);
    map.on('click', layer, show);
    map.on('mouseleave', layer, () => {
        map.getCanvas().style.cursor = '';
        popup.remove();
    });

    async function refresh() {
        try {
            const response = await fetch(config.endpoint);
            if (response.ok) {
                map.getSource(source).setData(await response.json());
            }
        } catch (error) {
            // keep the vessels of the last successful poll
        }
    }

    return {
        setVisible(show) {
            visible = show;
            map.setLayoutProperty(layer, 'visibility', show ? 'visible' : 'none');
            clearInterval(timer);
            timer = null;
            if (show) {
                refresh();
                timer = setInterval(refresh, config.pollMs);
            } else {
                popup.remove();
            }
        },
        get visible() {
            return visible;
        },
    };
}
