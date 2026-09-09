# Regulator's operator console

The web interface the control plane serves, built from Splunk's own React
component library so that it looks like the product it drives load at.

## Build

```bash
npm install       # or `make ui-deps` from the repo root
npm run build     # compiles into ../server/ui, which the control plane serves
npm run start     # the same, rebuilding on every change
npm run lint      # Splunk's eslint config, the same set CIMplicity uses
npm run smoke     # renders every page of the built bundle in jsdom
```

`../server/ui` is generated. It is not tracked in git, and nothing in it is
hand-edited: `index.html` is emitted by the build and names a content-hashed
bundle under `assets/`. The Docker image builds this in its own Node stage, so
the image that runs has no Node in it.

## Where things are

| Path | What it holds |
|---|---|
| `src/App.jsx` | Theme, session, sidebar, routing |
| `src/api.js` | The single place that talks to the control plane; a 401 anywhere sends the whole app back to the sign-in screen |
| `src/format.js` | Numbers, bytes, durations and timestamps, so a p95 reads the same everywhere |
| `src/Notifications.jsx` | Page-level feedback, as a `MessageBar` |
| `src/useLoader.js` | Load-once-and-reload, the shape every list page needs |
| `src/useHashRoute.js` | Twenty lines of hash routing, in place of a router dependency |
| `src/components/` | The shared pieces: chips, tiles, panels, tables, charts |
| `src/pages/` | One file per route |
| `src/modals/` | One file per dialog |

## Conventions

**Use a Splunk component wherever one exists.** `Table`, `Modal`, `Card`,
`ControlGroup`, `Text`, `Number`, `Select`, `Checkbox`, `Chip`, `Message`,
`MessageBar`, `DefinitionList`, `Code`, `Menu`, `Progress`, `WaitSpinner` and
the rest are all in `@splunk/react-ui`, and the console should not be growing
its own version of any of them.

**Never hard-code a colour, a size or a font.** Everything comes from
`@splunk/themes`, either as a `variables.*` interpolation inside a
styled-component or from `useSplunkTheme()` where a value is needed in
JavaScript (the charts). This is what makes the light theme work without anyone
maintaining a second palette.

**Toasts are deprecated in Splunk UI and are not used here.** Feedback goes
through `useNotify()`, which renders a `MessageBar` at the top of the page: a
message that removes itself is a message somebody never received.

**Say what a number means, next to the number.** The prose in this console is
not decoration. A p95 with no statement about whether the cache was cold, or
whether the target was at its concurrency ceiling, is a number two people will
read differently.

## The charts

`src/components/LineChart.jsx` wraps `@splunk/visualizations/Line`, the same
component Dashboard Studio renders, and is the only file that knows about
Splunk's dataSource contract. Everything upstream passes plain
`[epochSeconds, value]` arrays:

```jsx
<LineChart
    title="Throughput and in flight"
    leftTitle="searches/s"
    rightTitle="in flight"
    markers={[{ at: 1757000100, label: 'evict', kind: 'evict' }]}
    series={[
        { label: 'searches/s', points: [[1757000000, 4.1], [1757000030, 4.4]] },
        { label: 'in flight', axis: 'right', points: [...] },
        { label: 'ceiling 12', color: theme.errorColor, dashed: true, points: [...] },
    ]}
/>
```

How that maps onto Splunk's options:

| Here | There |
|---|---|
| `axis: 'right'` | `overlayFields` + `showOverlayY2Axis` |
| `dashed: true` | `lineDashStylesByField` |
| `color` | `seriesColorsByField` |
| `markers` | `annotationX`/`annotationLabel`/`annotationColor` arrays |
| a `null` value | `nullValueDisplay: 'gaps'` |
| the x value | the `_time` field, in ISO 8601, so the axis is a time axis |

**Do not set a colour unless it carries meaning.** A ceiling and an error rate
earn one; `p50` does not. Splunk's default categorical palette is exported as
`PALETTE` for the one case that needs it, which is pinning two series to the
same colour so they can differ by dash instead (`p95` and `p95 so far`).

The exception is `Sparkline`, still a hand-drawn SVG. It is a 120x26 glyph in a
table cell, one per row, and a chart instance per row of a fifty-row table is a
real cost for something with no axes, legend or tooltip to be inconsistent
about.

Pass annotations as plain arrays. Splunk's examples feed them from a second
dataSource through the dynamic-options DSL (`> annotation|seriesByIndex(0)`),
which is how a Studio dashboard binds a search to them; outside a dashboard
that form creates the annotation layer and leaves it empty, with no error
anywhere.

## Splunk Web's page globals

The charting bundle underneath `@splunk/visualizations` is the same code Splunk
Web ships, and it reads `window.locale_name` and `window.$C` **while it is
being evaluated**, not when a chart is first drawn. Nothing defines them
outside Splunk Web, so the import throws:

```
TypeError: window.locale_name is not a function
```

`src/splunk-web-globals.js` defines them and is imported first in `index.jsx`.
A shim applied after that import is a shim applied too late.

## Adding a page

1. Write it in `src/pages/`, fetching through `useLoader(() => api('/thing'))`.
2. Add it to `NAV` and to `Page` in `src/App.jsx`.
3. Add a check for it in `smoke.mjs`, with a fixture in `ROUTES`. The smoke
   test loads the built bundle into jsdom and walks every route: a page that is
   not in it is a page nothing notices the breaking of.
