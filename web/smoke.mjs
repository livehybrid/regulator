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
    planned: { load_model: 'closed', virtual_users: 2, duration_s: 600, workers: 2, pacing_s: 5 },
    stats: {
        executions: 1200,
        errors: 3,
        errors_by_class: { timeout: 2, server: 1 },
        unknown_sid: { searches: 3, responses: 5 },
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
                errors_by_class: { timeout: 1 },
                partial: 1,
                queued: 12,
                queued_p95_ms: 90,
                unknown_sid: { searches: 3, responses: 5 },
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
                scheduler: {
                    available: true,
                    description: 'Scheduled searches skipped or deferred during the run',
                    rows: [{ status: 'skipped', reason: 'maxconcurrent', count: '340' }],
                },
                our_searches_by_step: {
                    available: true,
                    description: 'The audit records split by step',
                    rows: [
                        {
                            step: 'triage.overview',
                            searches: '600',
                            run_time_s: '300',
                            searched_buckets: '4100',
                            eliminated_buckets: '9800',
                            cache_hits: '3888',
                            cache_misses: '212',
                            cache_miss_pct: '5.17',
                            cold_path_s: '123',
                            cold_path_pct: '41',
                        },
                    ],
                },
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
    // Configured AND writable, so the Scenarios page renders its source banner
    // and its Push button: the states that only appear when a shared library
    // is in play are the ones a render test is worth having for.
    '/scenario-source': {
        configured: true,
        writable: true,
        kind: 's3',
        location: 's3://regulator-scenarios/shared',
        error: null,
    },
    '/config/source': {
        configured: true,
        writable: true,
        kind: 's3',
        location: 's3://regulator-config/lab',
        object: 'regulator-config.json',
        error: null,
        carried: 1,
        last_pull: {
            at: '2026-10-28T09:00:00Z',
            ok: true,
            found: true,
            created: { targets: 1, scenarios: 2, baselines: 1, runs: 14 },
            skipped: 0,
        },
        last_push: null,
    },
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
            has_token: true,
            has_username: false,
            has_password: false,
            has_indexer_token: false,
            has_indexer_password: false,
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
        {
            name: 'pack-flatline',
            description: 'A pack whose data this lab does not hold.',
            origin: 'user',
            engine: 'api',
            load_model: 'closed',
            personas: 1,
            steps: 3,
            saved_searches: 3,
            virtual_users: 5,
            duration_s: 600,
            index: 'main',
            sourcetypes: ['stoker:flatline'],
            requires_packs: ['flatline'],
            tags: ['imported', 'savedsearches', 'closed', 'stoker-corpus', 'metrics'],
            lint: [],
            runnable_here: true,
        },
    ],
    // One reported target: the first scenario fits it, the second does not,
    // so the default filter has something to hide and say it hid.
    '/scenario-fit': {
        targets: [
            {
                id: 1,
                name: 'lab',
                reported_at: 1757000000,
                fit: {
                    'dashboard-triage': { fit: 'full', missing: [] },
                    'pack-flatline': { fit: 'none', missing: ['stoker:flatline'] },
                },
            },
        ],
    },
    '/runs': [
        {
            id: 7,
            label: 'smoke run',
            scenario: 'dashboard-triage',
            target_name: 'lab',
            state: 'completed',
            created_at: 1757000000,
            planned: { load_model: 'closed', virtual_users: 2, duration_s: 600, workers: 1 },
            headline: {
                executions: 1200,
                p95_ms: 1450,
                p99_ms: 2600,
                error_rate_pct: 0.25,
                searches_queued: 12,
                valid: true,
                cache_provenance: 'cold',
            },
        },
        {
            id: 6,
            label: 'one user',
            scenario: 'dashboard-triage',
            target_name: 'lab',
            state: 'completed',
            created_at: 1756990000,
            planned: { load_model: 'closed', virtual_users: 1, duration_s: 600, workers: 1 },
            headline: { executions: 600, p95_ms: 980, p99_ms: 1730, valid: true },
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
    // A running fleet run that is paused, with a live load change and a note.
    '/runs/8': {
        ...RUN,
        id: 8,
        label: 'paused run',
        state: 'running',
        fleet_state: 'running',
        paused: true,
        paused_s: 95,
        load_override: 40,
        note: 'held while indexer 3 restarts',
        summary: null,
    },
    // A run that warmed a confirmed plan before its load, for the run page's
    // note and for "Run again", which must show the plan and not re-confirm it.
    '/runs/9': {
        ...RUN,
        id: 9,
        label: 'warm start',
        warm_cache: true,
        warm_plan: {
            plan: [{ index: 'cultivar_web', earliest: '-7d@d', latest: 'now' }],
            mode: 'tsidx',
            concurrency: 4,
            budget_s: 1800,
        },
        summary: {
            ...RUN.summary,
            cache: {
                ...RUN.summary.cache,
                forecast: {
                    available: true,
                    buckets: 12,
                    local: 12,
                    local_pct: 100,
                    bytes_total: 9000000000,
                    bytes_remote: 0,
                    residency: { available: true, counts: { full: 0, index_only: 12, partial: 0, stub: 0 } },
                },
                warm: {
                    mode: 'tsidx',
                    searches: 7,
                    failed: 0,
                    not_started: 0,
                    skipped_local: 1,
                    seconds: 12.4,
                    before: { available: true, buckets: 12, local: 3, local_pct: 25 },
                    after: { available: true, buckets: 12, local: 12, local_pct: 100 },
                },
            },
        },
    },
    '/runs/7/samples': SAMPLES,
    '/targets/1': { id: 1, name: 'lab', mgmt_url: 'https://splunk.lab:8089', health: 'unknown' },
    '/targets/1/test': {
        ok: true,
        detail: 'Splunk 10.4.4 (search_head, shc_member)',
        version: '10.4.4',
        roles: ['search_head', 'shc_member'],
        cores: 2,
        max_hist_searches: 8,
    },
    '/targets/1/samples': SAMPLES,
    '/targets/1/cache': CACHE,
    '/targets/1/buckets': {
        available: true,
        detail: 'disk',
        states: ['remote', 'index_only', 'full'],
        earliest: 1757000000,
        latest: 1757000000 + 30 * 86400,
        bin_s: 43200,
        bins: 60,
        notes: [],
        indexes: [
            {
                index: 'cultivar_web',
                buckets: 3,
                bytes: 3000000000,
                local_pct: 66.67,
                undated: 0,
                outside: 0,
                by_state: {
                    remote: { buckets: 1, bytes: 1000000000 },
                    index_only: { buckets: 1, bytes: 1000000000 },
                    full: { buckets: 1, bytes: 1000000000 },
                },
                cells: [
                    { bin: 10, buckets: 1, bytes: { remote: 1000000000 } },
                    { bin: 40, buckets: 2, bytes: { index_only: 600000000, full: 400000000 } },
                    { bin: 59, buckets: 1, bytes: { full: 600000000 } },
                ],
            },
        ],
        overlay: {
            scenario: 'web-dashboards',
            duration_s: 600,
            spans: [{ index: 'cultivar_web', earliest: 1757000000 + 29 * 86400, latest: 1757000000 + 30 * 86400, clipped: false }],
            notes: [],
        },
    },
    '/targets/1/warm/suggest': {
        scenario: 'web-dashboards',
        duration_s: 600,
        mode: 'tsidx',
        plan: [
            {
                index: 'cultivar_web',
                earliest: 1757000000,
                latest: 1757086400,
                earliest_iso: '2025-09-04T15:33:20Z',
                latest_iso: '2025-09-05T15:33:20Z',
            },
        ],
        forecast: {
            available: true,
            buckets: 12,
            local: 3,
            local_pct: 25,
            bytes_total: 9000000000,
            bytes_remote: 6750000000,
            capacity_pct: 12,
            notes: ['step one: a macro hides what it expands to; its indexes and time bounds are not seen'],
            residency: {
                available: true,
                counts: { full: 1, index_only: 2, partial: 0, stub: 9 },
                bytes_outstanding: 7000000000,
            },
        },
    },
    // GET of the status and the POST that starts one both answer with the
    // job; this one has already finished, so the dialog renders the result.
    '/targets/1/warm': {
        state: 'done',
        mode: 'tsidx',
        plan: [{ index: 'cultivar_web' }],
        result: {
            searches: 1,
            seconds: 4.2,
            failed: 0,
            not_started: 0,
            skipped_local: 0,
            errors: [],
            before: { available: true, buckets: 12, local: 3, local_pct: 25, bytes_total: 9000000000 },
            after: { available: true, buckets: 12, local: 12, local_pct: 100, bytes_total: 9000000000 },
        },
    },
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
// Every write the console makes, so a dialog can be driven to its submit and
// what it sent asserted on.
const posted = [];
window.fetch = async (url, init = {}) => {
    if (init.method && init.method !== 'GET') {
        posted.push({ url: String(url), method: init.method, body: init.body ? JSON.parse(init.body) : null });
    }
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
    await check('scenarios: the source banner', 's3://regulator-scenarios/shared');
    await check('scenarios: the default filter hides what fits no target', '1 hidden because no target');
    await check('scenarios: the card says which target it fits', 'Fits');
    if ((window.document.body.textContent || '').includes('A pack whose data this lab')) {
        problems.push('scenarios: a scenario that fits no target was shown under the default filter');
    }
    clickByText('Clear filters', 'scenarios: clear filters');
    await check('scenarios: clearing the filter shows everything', 'A pack whose data this lab');
    await check('scenarios: a long tag list is folded', '+2');
    await check('scenarios: edit on the card', 'Edit');
    clickByText('More', 'scenarios: the card menu');
    await check('scenarios: the archive buttons', 'Download');
    await check('scenarios: push appears when the source is writable', 'Push');

    await go('#runs');
    await check('runs', 'smoke run');
    // The setting two runs differ by is a column, not a click away.
    await check('runs: load column, two users', '2 users');
    await check('runs: load column, one user', '1 user');
    clickByText('Columns', 'open the column picker');
    await settle();
    {
        const wrap = Array.from(window.document.querySelectorAll('[data-test="checkbox"]')).find(
            (n) => (n.textContent || '').trim() === 'p99'
        );
        const box = wrap && (wrap.querySelector('[role="checkbox"], input, button') || wrap);
        if (!box) {
            problems.push(`runs: column picker has no p99 option (${window.document.querySelectorAll('[data-test="checkbox"]').length} checkboxes)`);
        } else {
            box.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
            await settle();
            await check('runs: p99 column shown once ticked', '1.73 s');
            await waitFor('runs: the column choice is remembered', () =>
                (window.localStorage.getItem('regulator.runs.columns') || '').includes('p99')
            );
        }
    }

    await go('#run/7');
    await check('run: tiles', 'Peak in flight');
    await check('run: a finished run says how long it ran', 'of 10m 0s planned');
    await check('run: load tile', '2 workers · pacing 5.0 s');
    await check('run: queueing banner', 'Queueing observed');
    await check('run: cache provenance banner', 'Cache provenance: cold');
    await check('run: steps', 'triage.overview');
    await check('run: cold/warm columns', 'Cold p95');
    await check('run: workers', 'pod-a');
    await check('run: epochs', 'Cache epochs');
    await check('run: what the cluster said', 'What the cluster said');
    // The roll-up: what went wrong and on which step, and what it cost others.
    await check('run: search issues panel', 'Search issues');
    await check('run: unknown sids on their step', '3 / 5');
    await check('run: queueing on its step', '12 p95 90 ms');
    // Two errors on the step, one with a class: the other is not lost.
    await waitFor('run: an error with no class recorded is still counted', () =>
        Array.from(window.document.querySelectorAll('tr')).some((tr) => {
            const cells = Array.from(tr.querySelectorAll('td')).map((td) => (td.textContent || '').trim());
            return cells[0] === 'triage.overview' && cells.length === 11 && cells[5] === '1' && cells[10] === '1';
        })
    );
    await check('run: skipped scheduled searches', '340 skipped: maxconcurrent');
    await check('run: bucket attribution by step', 'Bucket attribution by step');
    await check('run: the share of run time spent waiting', '41%');
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

    // Run again opens the launch dialog filled in from this run; Edit opens
    // the label and note.
    clickByText('Run again', 'open run again');
    await check('run: run again is prefilled', 'Run again: smoke run');
    clickByText('Cancel', 'close run again');
    await settle();
    clickByText('Edit', 'open edit');
    await check('run: edit dialog', 'Edit run 7');
    clickByText('Cancel', 'close edit');
    await settle();

    await go('#run/8');
    await check('run: paused banner', 'the run clock is stopped');
    await check('run: time remaining', '5m 15s');
    await check('run: remaining clock stopped', 'clock stopped while paused');
    await check('run: note shown', 'held while indexer 3 restarts');
    await waitFor('run: resume button', () =>
        Array.from(window.document.querySelectorAll('button')).some((b) => (b.textContent || '').trim() === 'Resume')
    );

    await go('#target/1');
    await check('target detail', 'SmartStore cache');

    // The bucket map is read on demand, never on page load.
    await check('bucket map: panel', 'Bucket map');
    if ((window.document.body.textContent || '').includes('Index files only')) {
        problems.push('bucket map: read the buckets without being asked');
    }
    clickByText('Load bucket map', 'load the bucket map');
    await check('bucket map: legend', 'Index files only');
    await check('bucket map: scenario overlay in the legend', 'Read by web-dashboards');
    await check('bucket map: table view', '1 (953.7 MB)');

    // Warm-up: the suggestion fills an editable plan, and nothing is posted to
    // /warm until the operator confirms it.
    clickByText('Warm cache', 'open the warm dialog');
    await check('warm dialog', 'You decide what gets warmed');
    await waitFor('warm dialog: scenarios loaded', () =>
        Array.from(window.document.querySelectorAll('button')).some(
            (b) => (b.textContent || '').trim() === 'Suggest a plan' && !b.disabled
        )
    );
    clickByText('Suggest a plan', 'ask for a suggestion');
    await waitFor('warm dialog: the suggestion fills the plan', () =>
        Array.from(window.document.querySelectorAll('input')).some((i) => i.value === 'cultivar_web')
    );
    await check('warm dialog: residency shown', 'index files only');
    await check('warm dialog: what it could not see', 'a macro hides');
    const warmPosts = () => posted.filter((x) => x.url.endsWith('/targets/1/warm'));
    clickByText('Warm', 'warm before confirming');
    await settle(300);
    if (warmPosts().length) {
        problems.push('warm dialog: posted a warm-up before the plan was confirmed');
    } else {
        process.stdout.write('  ok  warm dialog: nothing warmed unconfirmed\n');
    }
    // Splunk's Checkbox is a wrapper carrying data-test-value, around a toggle button.
    const box = window.document.querySelector('[data-test="checkbox"][data-test-value="confirmed"]');
    const confirm = box && (box.querySelector('[data-test="toggle"]') || box.querySelector('button'));
    if (!confirm) {
        problems.push('warm dialog: no confirmation checkbox');
    } else {
        confirm.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
        await settle(100);
        clickByText('Warm', 'warm the confirmed plan');
        await waitFor('warm dialog: posts the confirmed plan', () =>
            warmPosts().some(
                (x) => x.body && x.body.confirmed === true && x.body.plan[0].index === 'cultivar_web'
            )
        );
        await check('warm dialog: result', 'Plan, after');
    }
    clickByText('Close', 'close the warm dialog');
    await settle(100);

    await go('#baselines');
    await check('baselines', 'main-green');

    await go('#audit');
    await check('audit', 'run_launched');

    await go('#config');
    await check('configuration', 'Download config');
    await check('configuration: the restore half', 'Choose a config file');
    // The master-key warning is the whole reason the download is worth
    // explaining, so it must survive a refactor of the page.
    await check('configuration: master key note', 'REG_MASTER_KEY');
    await check('configuration: names the shared source', 's3://regulator-scenarios/shared');
    await check('configuration: shared copy location', 's3://regulator-config/lab/regulator-config.json');
    await check('configuration: last pull counts runs', '14 run(s) new');
    await check('configuration: carried rows explained', 'kept in every push');

    // Dialogs are where a component library is most easily misused (a Modal
    // without returnFocus, a Select given children it will not accept), so the
    // two most-used ones are opened rather than only rendered behind a click
    // nobody makes here.
    await go('#targets');
    clickByText('Add target', 'open the add-target form');
    await settle();
    await check('add target form', 'A bearer token is preferred');

    clickByText('Cancel', 'close the add-target form');
    await settle();

    // Test lays out what it found as labelled facts, not one squashed line.
    clickByText('Test', 'test a target');
    await check('test result: version', '10.4.4');
    await check('test result: ceiling', '8 concurrent historical searches');
    await check('test result: labelled', 'Search ceiling');

    // Edit a target: replace an expired token without retyping anything else.
    {
        const row = Array.from(window.document.querySelectorAll('tr')).find((r) =>
            (r.textContent || '').includes('https://splunk.lab:8089')
        );
        const toggle = row && row.querySelector('[data-test="actions-secondary-toggle"]');
        if (!toggle) {
            problems.push('targets: no row actions menu');
        } else {
            toggle.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
            await settle();
            clickByText('Edit…', 'open the edit-target form');
            await check('edit target form', 'leave a credential blank to keep the stored one');
            await check('edit target form: stored token', 'Remove the stored token');
            const label = Array.from(window.document.querySelectorAll('label')).find(
                (n) => (n.textContent || '').trim() === 'Bearer token'
            );
            const group = label && label.closest('[data-test="control-group"]');
            const input = group && group.querySelector('input');
            if (!input) {
                problems.push('edit target form: no token input');
            } else {
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(input, 'a-fresh-token');
                input.dispatchEvent(new window.Event('input', { bubbles: true }));
                input.dispatchEvent(new window.Event('change', { bubbles: true }));
                await settle(100);
                clickByText('Save changes', 'save the target');
                await waitFor('edit target: sends only the new token', () =>
                    posted.some(
                        (p) =>
                            p.method === 'PATCH' &&
                            p.url.endsWith('/targets/1') &&
                            JSON.stringify(p.body) === JSON.stringify({ token: 'a-fresh-token' })
                    )
                );
            }
        }
    }

    clickByText('Launch a run', 'open the launch dialog');
    await settle(400);
    await check('launch dialog', 'Pacing decides what the latency numbers mean');

    // Type a user count and press Launch. Converting a typed number is what
    // crashed (React error #321): the number input was imported as `Number`,
    // shadowing the global, so Number(v) called the component as a function.
    {
        const label = Array.from(window.document.querySelectorAll('label')).find(
            (n) => (n.textContent || '').trim() === 'Virtual users'
        );
        const group = label && label.closest('[data-test="control-group"]');
        const input = group && group.querySelector('input');
        if (!input) {
            problems.push('launch dialog: no virtual users input');
        } else {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(input, '25');
            input.dispatchEvent(new window.Event('input', { bubbles: true }));
            input.dispatchEvent(new window.Event('change', { bubbles: true }));
            input.dispatchEvent(new window.FocusEvent('blur', { bubbles: true }));
            await settle(200);
            clickByText('Launch', 'press Launch');
            await waitFor('launch dialog: posts the typed user count', () =>
                posted.some((p) => p.url.endsWith('/runs') && p.body && p.body.virtual_users === 25)
            );
        }
    }

    await settle(200);

    // A run that warmed says so, and "Run again" brings its plan back as it
    // was written, unconfirmed: nothing is launched until it is ticked again.
    await go('#run/9');
    await check('warm run: the run page says what the warm-up did', 'Warmed before the run from a confirmed plan');
    await check('warm run: the forecast at the start', 'Forecast at the start: this run reads 12 bucket(s)');
    await check('warm run: the forecast says what is on disk', '12 index files only');
    clickByText('Run again', 'open run again for the warm run');
    await check('warm run: run again shows the warm-up', 'You decide what gets warmed');
    await waitFor('warm run: the plan comes back as written', () =>
        Array.from(window.document.querySelectorAll('input')).some((i) => i.value === '-7d@d')
    );
    await check('warm run: the plan must be checked again', 'check it again');
    {
        const warmRuns = () => posted.filter((x) => x.url.endsWith('/runs') && x.body && x.body.warm_cache);
        clickByText('Launch', 'launch before confirming the plan');
        await settle(300);
        if (warmRuns().length) {
            problems.push('launch dialog: launched a warm run before the plan was confirmed');
        } else {
            process.stdout.write('  ok  launch dialog: no warm run launched unconfirmed\n');
        }
        const tick = window.document.querySelector('[data-test="checkbox"][data-test-value="warm-confirmed"]');
        const toggle = tick && (tick.querySelector('[data-test="toggle"]') || tick.querySelector('button'));
        if (!toggle) {
            problems.push('launch dialog: no warm-up confirmation checkbox');
        } else {
            toggle.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
            await settle(100);
            clickByText('Launch', 'launch the confirmed warm run');
            await waitFor('launch dialog: posts the confirmed plan as written', () =>
                warmRuns().some(
                    (x) =>
                        x.body.warm_confirmed === true &&
                        x.body.warm_plan.length === 1 &&
                        x.body.warm_plan[0].index === 'cultivar_web' &&
                        x.body.warm_plan[0].earliest === '-7d@d' &&
                        !x.body.evict_cache
                )
            );
        }
    }

    await settle(200);

    if (problems.length) {
        console.error('\nsmoke FAILED:');
        problems.forEach((p) => console.error(`  - ${p}`));
        process.exit(1);
    }
    console.log('\nsmoke passed');
    process.exit(0);
})();
