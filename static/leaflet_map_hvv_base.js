/* Minimal base map for the HVV live vehicle page: one vector basemap layer
 * (Positron), attribution incl. privacy link, centered on Hamburg. Separate
 * from leaflet_map_offline.js on purpose - that one is also used by the
 * editor and carries features this page doesn't need.
 */

const minZoom = 9;
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
