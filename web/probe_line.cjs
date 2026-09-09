// The charting bundle reaches for window/document at import time (it carries
// jQuery), so the DOM has to exist before the require, not after it.
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/',
    pretendToBeVisual: true,
});
global.window = dom.window;
global.document = dom.window.document;
global.navigator = dom.window.navigator;
global.self = dom.window;
global.HTMLElement = dom.window.HTMLElement;
global.Element = dom.window.Element;
global.Node = dom.window.Node;
global.SVGElement = dom.window.SVGElement;
global.getComputedStyle = dom.window.getComputedStyle;
global.requestAnimationFrame = (cb) => setTimeout(cb, 0);
global.cancelAnimationFrame = clearTimeout;
dom.window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
global.ResizeObserver = dom.window.ResizeObserver;
dom.window.matchMedia = (q) => ({ matches: false, media: q, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} });
global.matchMedia = dom.window.matchMedia;
// Splunk Web defines these on the page; the charting bundle expects them.
dom.window.locale_name = () => 'en_GB';
dom.window.$C = { LOCALE: 'en-GB', MRSPARKLE_ROOT_PATH: '', SPLUNKD_PATH: '' };
global.locale_name = dom.window.locale_name;
global.$C = dom.window.$C;
// uplot reads a bare `devicePixelRatio`; in a browser that is on window, but
// this probe requires the module in Node's CJS scope.
global.devicePixelRatio = 1;

const React = require('react');
const { createRoot } = require('react-dom/client');
const Line = require('@splunk/visualizations/Line').default;
const { SplunkThemeProvider } = require('@splunk/themes');

const el = React.createElement(
    SplunkThemeProvider,
    { family: 'enterprise', colorScheme: 'dark', density: 'compact' },
    React.createElement(Line, {
        width: 640,
        height: 220,
        options: {
            overlayFields: ['in flight'],
            showOverlayY2Axis: true,
            seriesColorsByField: { 'searches/s': '#0371ab', 'in flight': '#dd9900', ceiling: '#dc4e41' },
            lineDashStylesByField: { ceiling: 'shortDash' },
            nullValueDisplay: 'gaps',
            legendDisplay: 'bottom',
            backgroundColor: 'transparent',
            yAxisTitleText: 'searches/s',
            y2AxisTitleText: 'in flight',
            annotationX: '> annotation|seriesByIndex(0)',
            annotationLabel: '> annotation|seriesByIndex(1)',
            annotationColor: '> annotation|seriesByIndex(2)',
        },
        dataSources: {
            primary: {
                data: {
                    fields: [{ name: '_time' }, { name: 'searches/s' }, { name: 'in flight' }, { name: 'ceiling' }],
                    columns: [
                        ['2026-09-09T06:00:00.000Z', '2026-09-09T06:00:30.000Z', '2026-09-09T06:01:00.000Z'],
                        ['4.1', '4.4', null],
                        ['8', '9', '11'],
                        ['12', '12', '12'],
                    ],
                },
                meta: { totalCount: 3 },
            },
            annotation: {
                data: {
                    fields: [{ name: '_time' }, { name: 'annotation_label' }, { name: 'annotation_color' }],
                    columns: [['2026-09-09T06:00:30.000Z'], ['evict'], ['#f8be34']],
                },
                meta: { totalCount: 1 },
            },
        },
    })
);

const errs = [];
const origError = console.error;
console.error = (...a) => errs.push(String(a[0]).slice(0, 300));

const root = createRoot(document.getElementById('root'));
root.render(el);

setTimeout(() => {
    console.error = origError;
    const html = document.getElementById('root').innerHTML;
    console.log('html length:', html.length);
    console.log('has <svg>:', html.includes('<svg'));
    console.log('has highcharts:', /highcharts/i.test(html));
    console.log('series paths:', (html.match(/<path/g) || []).length);
    console.log('text mentions evict:', html.includes('evict'));
    console.log('text mentions in flight:', html.includes('in flight'));
    console.log('--- console.error captured ---');
    errs.slice(0, 8).forEach((e) => console.log('  ', e));
    console.log('snippet:', html.slice(0, 300));
    process.exit(0);
}, 3000);
