/*
 * A smoke test for the built console.
 *
 * It loads the actual bundle from server/ui into jsdom against a stubbed API,
 * walks every route, and fails on the first React error, unhandled rejection
 * or console.error. That is deliberately the built artefact rather than the
 * sources: what ships is a bundle, and the failures worth catching here (a
 * component imported from the wrong path, a prop the library rejects, an
 * undefined read while rendering a table) only appear once it runs.
 *
 *   node smoke.mjs
 *
 * It is not a substitute for opening the page. It is the check that stops a
 * broken build reaching someone who then has to open the page to find out.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM, ResourceLoader, VirtualConsole } from 'jsdom';

const here = path.dirname(fileURLToPath(import.meta.url));
const uiDir = path.resolve(here, '..', 'server', 'ui');

const rawHtml = fs.readFileSync(path.join(uiDir, 'index.html'), 'utf8');

/*
 * The entry bundle is whichever script the built page actually names, read out
 * of index.html rather than guessed from the directory. There are several
 * files in assets/ now that the chart library is a lazily-loaded chunk, and
 * picking the first one alphabetically picks the wrong one.
 */
const entrySrc = (rawHtml.match(/src="?(\/assets\/[^" >]+)/) || [])[1];
if (!entrySrc) {
    console.error('index.html names no bundle: run `npm run build` first');
    process.exit(1);
}
const bundle = fs.readFileSync(path.join(uiDir, entrySrc.replace(/^\//, '')), 'utf8');

// The entry is evaluated by hand below, after the browser shims are in place,
// so the page must not also load it itself.
const html = rawHtml.replace(/<script[^>]*src="?\/assets\/[^>]*><\/script>/, '');

/*
 * Serve /assets from the build output.
 *
 * The chart library is a dynamic import, so webpack loads it at runtime by
 * appending a <script> to the page. Without this, that request goes nowhere
 * and the charts silently never appear, which is exactly the failure this test
 * exists to catch.
 */
class BuildOutputLoader extends ResourceLoader {
    fetch(url) {
        const { pathname } = new URL(url);
        if (pathname.startsWith('/assets/')) {
            return Promise.resolve(fs.readFileSync(path.join(uiDir, pathname.slice(1))));
        }
        return null;
    }
}

/* ------------------------------------------------------------- fixtures */

const RUN = {
    id: 7,
    label: 'smoke run',
    scenario: 'dashboard-triage',
    target_id: 1,
    target_name: 'lab',
    state: 'completed',
    fleet: 'k8s',
    fleet_state: 'done',
    created_at: 1757000000,
    started_at: 1757000000,
    cold_window_s: 300,
    stats: {
        executions: 1200,
        errors: 3,
        error_rate_pct: 0.25,
        throughput_per_s: 4.2,
        elapsed_s: 285,
        peak_in_flight: 11,
        partial: 1,
        abandoned: 1,
        latency: { p50_ms: 320, p95_ms: 1450, p99_ms: 2600, max_ms: 4100, mean_ms: 480, count: 1200 },
        queueing: { searches_queued: 12, queued_pct: 1.0, queued_ms: { p95_ms: 90 } },
        target: { max_hist_searches: 12 },
        workers: [
            {
                slot: 0,
                engine: 'splunk',
                state: 'done',
                holder: 'pod-a',
                executions: 600,
                errors: 1,
                p95_ms: 1400,
                last_heartbeat_at: 1757000200,
                restarts: 1,
            },
        ],
        epochs: [{ epoch: 1, executions: 400, latency: { p50_ms: 500, p95_ms: 2000 } }],
        steps: [
            {
                step_id: 'triage.overview',
                class: 'dashboard',
                executions: 600,
                errors: 2,
                error_rate_pct: 0.33,
                latency: { p50_ms: 300, p95_ms: 1500 },
                dispatch: { p95_ms: 40 },
                ttfr: { p95_ms: 800 },
                scan_count_total: 120000,
                mean_events_per_s: 4200,
                cold: { p95_ms: 2400, count: 100 },
                warm: { p95_ms: 900, count: 500 },
            },
        ],
    },
    summary: {
        valid: true,
        outcome: 'completed',
        load_model: 'closed',
        effective_seed: 42,
        scenario_digest: 'abc123',
        co_corrected: false,
        self_instrumented: true,
        peak_virtual_users: 10,
        workers: [],
        cache: {
            delta: { provenance: 'cold', buckets_downloaded: 40, bytes_downloaded: 1024 * 1024 * 900 },
            epochs: [
                {
                    epoch: 1,
                    requested_at: 1757000100,
                    source: 'schedule',
                    attempted: 100,
                    confirmed: 98,
                    bytes_evicted: 1024 * 1024 * 500,
                    duration_s: 4.2,
                    note: 'periodic',
                },
            ],
        },
        sut: {
            findings: ['Two scheduled searches were skipped while this run was in flight.'],
            probes: {
                skipped_searches: {
                    available: true,
                    description: 'Scheduler skips during the run',
                    rows: [{ savedsearch_name: 'nightly rollup', reason: 'max concurrent' }],
                },
                unavailable_probe: { available: false, reason: 'no access to _internal' },
            },
        },
    },
};

const CACHE = {
    available: true,
    local_buckets: 120,
    remote_buckets: 400,
    total_buckets: 520,
    local_pct: 23,
    local_bytes: 1024 * 1024 * 900,
    total_bytes: 1024 * 1024 * 4000,
    max_cache_size_mb: 2048,
    fill_pct: 96,
    eviction_policy: 'lru',
    per_index: { main: { local_buckets: 100, remote_buckets: 300, local_pct: 25, local_bytes: 1024 * 1024 * 700 } },
};

const SAMPLES = {
    started_at: 1757000000,
    // Inside the sample range on purpose: an annotation beyond the axis
    // extent is dropped by the chart, so a marker at t+100 against samples
    // ending at t+90 would test nothing.
    markers: [{ at: 1757000060, label: 'evict', kind: 'evict' }],
    samples: [0, 30, 60, 90].map((d) => ({
        at: 1757000000 + d,
        slot: 0,
        in_flight: 8,
        kind: d === 60 ? 'evict' : 'poll',
        local_pct: 20 + d / 10,
        fill_pct: 50 + d / 10,
        local_buckets: 100,
        total_buckets: 500,
        local_bytes: 1024 * 1024 * 500,
        run_id: 7,
        interval: {
            throughput_per_s: 4 + d / 100,
            p50_ms: 300,
            p95_ms: 1400,
            p99_ms: 2200,
            error_rate_pct: 0.2,
            queued_pct: 1,
            queued_p95_ms: 80,
            loop_lag_p95_ms: 3,
            percentiles_note: 'busiest worker',
        },
        cum: { p95_ms: 1500 },
    })),
};

const ROUTES = {
    '/auth/status': { authenticated: true, setup_needed: false },
    '/targets': [
        {
            id: 1,
            name: 'lab',
            mgmt_url: 'https://splunk.lab:8089',
            web_url: 'https://splunk.lab:8000',
            app: 'search',
            owner: 'nobody',
            indexer_urls: 'https://idx1:8089,https://idx2:8089',
            verify_tls: true,
            health: 'ok',
            health_detail: 'reachable',
            created_at: 1757000000,
        },
    ],
    '/scenarios': [
        {
            name: 'dashboard-triage',
            description: 'Analysts opening dashboards.',
            origin: 'builtin',
            engine: 'browser',
            load_model: 'closed',
            personas: 2,
            steps: 6,
            saved_searches: 0,
            virtual_users: 10,
            duration_s: 600,
            index: 'main',
            sourcetypes: ['access_combined'],
            requires_packs: [],
            tags: ['dashboards'],
            lint: ['no corpus check declared'],
            runnable_here: true,
        },
    ],
    '/runs': [
        {
            id: 7,
            label: 'smoke run',
            scenario: 'dashboard-triage',
            target_name: 'lab',
            state: 'completed',
            created_at: 1757000000,
            headline: {
                executions: 1200,
                p95_ms: 1450,
                error_rate_pct: 0.25,
                searches_queued: 12,
                valid: true,
                cache_provenance: 'cold',
            },
        },
    ],
    '/runs/sparklines': { 7: [100, 200, 180, 260] },
    '/baselines': [
        { label: 'main-green', run_id: 7, scenario: 'dashboard-triage', created_at: 1757000000, note: 'before the change' },
    ],
    '/audit': {
        events: [
            { at: 1757000000, action: 'run_launched', actor: 'admin', client: '10.0.0.1', target_id: 1, detail: 'scenario dashboard-triage' },
        ],
    },
    '/fleets': [{ kind: 'k8s', available: true, default: true, detail: 'in-cluster' }],
    '/runs/7': RUN,
    '/runs/7/samples': SAMPLES,
    '/targets/1/samples': SAMPLES,
    '/targets/1/cache': CACHE,
};

function lookup(url) {
    const route = url.replace(/^\/api/, '').split('?')[0];
    if (route in ROUTES) {
        return ROUTES[route];
    }
    if (route.endsWith('/samples')) {
        return SAMPLES;
    }
    return null;
}

/* ------------------------------------------------------------- harness */

const problems = [];

/*
 * jsdom implements no canvas and no layout engine. The chart library touches
 * both, and neither is a defect in the console: nothing here asserts on pixels,
 * and the accelerated canvas render path is off. Everything else still fails
 * the run, so this stays a short, named list rather than a blanket ignore.
 */
const EXPECTED_IN_JSDOM = [
    'Not implemented: HTMLCanvasElement.prototype.getContext',
    'Could not parse CSS stylesheet',
];
const expected = (message) => EXPECTED_IN_JSDOM.some((known) => String(message).includes(known));

const virtualConsole = new VirtualConsole();
virtualConsole.on('error', (m) => {
    if (!expected(m)) {
        problems.push(`console.error: ${m}`);
    }
});
virtualConsole.on('jsdomError', (e) => {
    if (!expected(e.message)) {
        problems.push(`jsdom: ${e.message}`);
    }
});
virtualConsole.on('warn', () => {});
virtualConsole.on('log', () => {});
virtualConsole.on('info', () => {});

const dom = new JSDOM(html, {
    url: 'http://localhost/',
    // 'dangerously' rather than 'outside-only' so that the <script> webpack
    // appends for the chart chunk is actually executed. The only thing served
    // is this repository's own build output.
    runScripts: 'dangerously',
    resources: new BuildOutputLoader(),
    pretendToBeVisual: true,
    virtualConsole,
});
const { window } = dom;

// Pieces of a browser that jsdom does not implement but the component library
// legitimately uses.
window.matchMedia =
    window.matchMedia ||
    ((q) => ({
        matches: false,
        media: q,
        addListener() {},
        removeListener() {},
        addEventListener() {},
        removeEventListener() {},
    }));
window.scrollTo = () => {};
window.HTMLElement.prototype.scrollIntoView = () => {};
// Highcharts measures text through getBBox, which jsdom does not implement.
// Returning zeros is enough for it to lay a chart out; nothing here asserts on
// pixel positions.
window.SVGElement.prototype.getBBox =
    window.SVGElement.prototype.getBBox || (() => ({ x: 0, y: 0, width: 0, height: 0 }));
// The charts are sized from their container, and jsdom reports every element
// as zero-sized, so ResizeObserver has to report something for one to draw.
window.ResizeObserver = class {
    constructor(callback) {
        this.callback = callback;
    }

    observe(target) {
        this.callback([{ target, contentRect: { width: 640, height: 220 } }], this);
    }

    unobserve() {}

    disconnect() {}
};
Object.defineProperty(window.HTMLElement.prototype, 'clientWidth', {
    configurable: true,
    get() {
        return 640;
    },
});
Object.defineProperty(window.HTMLElement.prototype, 'clientHeight', {
    configurable: true,
    get() {
        return 220;
    },
});
window.HTMLElement.prototype.getBoundingClientRect = () => ({
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    right: 640,
    bottom: 220,
    width: 640,
    height: 220,
});
window.fetch = async (url) => {
    const body = lookup(String(url));
    if (body === null) {
        return {
            ok: false,
            status: 404,
            statusText: 'Not Found',
            text: async () => JSON.stringify({ detail: `no fixture for ${url}` }),
        };
    }
    return { ok: true, status: 200, statusText: 'OK', text: async () => JSON.stringify(body) };
};

window.addEventListener('error', (e) => problems.push(`window error: ${e.message}`));
window.addEventListener('unhandledrejection', (e) =>
    problems.push(`unhandled rejection: ${e.reason}`)
);

window.eval(bundle);

const settle = (ms = 250) => new Promise((r) => setTimeout(r, ms));

/*
 * Wait for text rather than sleeping for a fixed time. Every page here fetches
 * before it can render anything, so a fixed sleep is a race that passes on a
 * quiet machine and fails on a busy one, which is the worst kind of test.
 */
const check = async (label, needle, timeoutMs = 5000) => {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
        if ((window.document.body.textContent || '').includes(needle)) {
            process.stdout.write(`  ok  ${label}\n`);
            return;
        }
        if (Date.now() > deadline) {
            problems.push(`${label}: expected to find ${JSON.stringify(needle)} on the page`);
            return;
        }
        await settle(50); // eslint-disable-line no-await-in-loop
    }
};

/** Click the first button whose label matches, so the dialogs get exercised. */
const clickByText = (label, what) => {
    const el = Array.from(window.document.querySelectorAll('button, a')).find(
        (n) => (n.textContent || '').trim() === label
    );
    if (!el) {
        problems.push(`${what}: no clickable element labelled ${JSON.stringify(label)}`);
        return;
    }
    el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
};

/** The same wait, for a condition that is not a piece of text. */
const waitFor = async (label, predicate, timeoutMs = 8000) => {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
        if (predicate()) {
            process.stdout.write(`  ok  ${label}\n`);
            return;
        }
        if (Date.now() > deadline) {
            problems.push(`${label}: the condition never became true`);
            return;
        }
        await settle(50); // eslint-disable-line no-await-in-loop
    }
};

const go = async (hash) => {
    window.location.hash = hash;
    window.dispatchEvent(new window.Event('hashchange'));
    await settle();
};

(async () => {
    await settle(400);
    await check('targets', 'lab');
    await check('targets: management url', 'https://splunk.lab:8089');

    await go('#scenarios');
    await check('scenarios', 'dashboard-triage');
    await check('scenarios: lint on the card', 'no corpus check declared');

    await go('#runs');
    await check('runs', 'smoke run');

    await go('#run/7');
    await check('run: tiles', 'Peak in flight');
    await check('run: queueing banner', 'Queueing observed');
    await check('run: cache provenance banner', 'Cache provenance: cold');
    await check('run: steps', 'triage.overview');
    await check('run: cold/warm columns', 'Cold p95');
    await check('run: workers', 'pod-a');
    await check('run: epochs', 'Cache epochs');
    await check('run: what the cluster said', 'What the cluster said');
    /*
     * The charts are Splunk Line components, loaded as a dynamic chunk. This
     * first check is the one with a long deadline: it covers fetching that
     * chunk off disk, evaluating it, measuring the container and drawing.
     */
    await waitFor(
        'run: charts drew',
        () => window.document.querySelectorAll('svg').length > 0,
        30000
    );

    /*
     * Then the mapping itself, because "an svg exists" would still pass with
     * every series silently dropped. These four are one each of the things
     * this file translates into Splunk's options, and each would disappear on
     * its own if that translation broke:
     *
     *   searches/s   a plain series on the primary axis
     *   in flight    overlayFields + showOverlayY2Axis, the second axis
     *   ceiling 12   a dashed reference series, from lineDashStylesByField
     *   evict        an annotation, drawn by the library
     *
     * The annotation is checked through the DOM rather than by looking for its
     * label in text: Splunk draws one as a dashed rule with the label on
     * hover, so there is no static text to find. Asserting on the text is how
     * this test spent a while reporting a failure that was not one.
     */
    const svgText = () =>
        Array.from(window.document.querySelectorAll('svg text')).map((t) =>
            (t.textContent || '').trim()
        );
    await waitFor('run: chart series', () => svgText().includes('searches/s'));
    await waitFor('run: chart overlay axis', () => svgText().includes('in flight'));
    await waitFor('run: chart reference line', () => svgText().includes('ceiling 12'));
    await waitFor(
        'run: chart annotation',
        () => window.document.querySelectorAll('.highcharts-splunk-annotation').length > 0
    );

    await go('#target/1');
    await check('target detail', 'SmartStore cache');

    await go('#baselines');
    await check('baselines', 'main-green');

    await go('#audit');
    await check('audit', 'run_launched');

    // Dialogs are where a component library is most easily misused (a Modal
    // without returnFocus, a Select given children it will not accept), so the
    // two most-used ones are opened rather than only rendered behind a click
    // nobody makes here.
    await go('#targets');
    clickByText('Add target', 'open the add-target form');
    await settle();
    await check('add target form', 'A bearer token is preferred');

    clickByText('Launch a run', 'open the launch dialog');
    await settle(400);
    await check('launch dialog', 'Pacing decides what the latency numbers mean');

    await settle(200);

    if (problems.length) {
        console.error('\nsmoke FAILED:');
        problems.forEach((p) => console.error(`  - ${p}`));
        process.exit(1);
    }
    console.log('\nsmoke passed');
    process.exit(0);
})();
