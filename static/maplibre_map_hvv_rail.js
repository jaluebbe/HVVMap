// Optional OpenRailwayMap overlay from /api/vector/overlay/style.json.
// Off by default; only offered if an overlay .mbtiles file is mounted.
// Used by maplibre_map_hvv_base.js.

const RAIL_STYLE_ENDPOINT = '/api/vector/overlay/style.json';
// Basemap railway lines the overlay replaces; hidden while it is shown.
const RAIL_BASEMAP_LAYERS = [
    'railway_transit', 'railway_transit_dashline', 'railway_service',
    'railway_service_dashline', 'railway', 'railway_dashline',
];

// Above the basemap's roads, rails and street names, below its place names
// and the live layers; rail labels win collisions with street names.
function railInsertBeforeId(map) {
    const layers = map.getStyle().layers;
    let lastTransport = -1;
    layers.forEach((l, i) => {
        if (['transportation', 'transportation_name'].includes(l['source-layer'])) {
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
    function setVisible(show) {
        const visibility = show ? 'visible' : 'none';
        layerIds.forEach((id) => map.setLayoutProperty(id, 'visibility', visibility));
        RAIL_BASEMAP_LAYERS.filter((id) => map.getLayer(id)).forEach((id) => {
            map.setLayoutProperty(id, 'visibility', show ? 'none' : 'visible');
        });
    }
    setVisible(false);
    return { setVisible };
}
