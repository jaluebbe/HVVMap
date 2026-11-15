// Optional rail network overlay from /api/vector/overlay/style.json.
// Visible by default; only offered if an overlay .mbtiles file is mounted.
// Used by maplibre_map_hvv_base.js.

const RAIL_STYLE_ENDPOINT = '/api/vector/overlay/style.json';

// Above the basemap's roads and rails, below its labels and the live layers.
function railInsertBeforeId(map) {
    const layers = map.getStyle().layers;
    let lastTransport = -1;
    layers.forEach((l, i) => {
        if (l['source-layer'] === 'transportation' && l.type !== 'symbol') {
            lastTransport = i;
        }
    });
    const next = layers.find((l, i) => i > lastTransport && l.type === 'symbol' && !l.id.startsWith('hvv-'));
    const live = layers.find((l) => l.id.startsWith('hvv-'));
    return (next || live || {}).id;
}

// Resolves to null while no overlay is available.
async function createHvvRailLayer(map) {
    let style;
    try {
        const response = await fetch(RAIL_STYLE_ENDPOINT);
        if (!response.ok) {
            return null;
        }
        style = await response.json();
    } catch (error) {
        return null;
    }
    Object.entries(style.sources).forEach(([id, source]) => map.addSource(id, source));
    const beforeId = railInsertBeforeId(map);
    const layerIds = style.layers.map((layer) => {
        map.addLayer(layer, beforeId);
        return layer.id;
    });
    return {
        setVisible(show) {
            layerIds.forEach((id) => map.setLayoutProperty(id, 'visibility', show ? 'visible' : 'none'));
        },
    };
}
