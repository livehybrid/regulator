/*
 * The Splunk Line chart itself, in its own module so it can be loaded on
 * demand.
 *
 * @splunk/visualizations is by far the largest thing this console depends on:
 * the charting bundle underneath it carries Highcharts and its own copy of
 * jQuery. Most of the console has no chart on it at all (targets, scenarios,
 * baselines, audit, and the sign-in screen), so this is split out and imported
 * lazily by LineChart. Pages that draw nothing never download it, and the
 * initial page load stays small.
 */
import React, { useMemo } from 'react';
import PropTypes from 'prop-types';
import Line from '@splunk/visualizations/Line';
import { useSplunkTheme } from '@splunk/themes';

/** Splunk wants ISO 8601 on `_time`; the API gives epoch seconds. */
const iso = (epochSeconds) => new Date(epochSeconds * 1000).toISOString();

/**
 * Turn the series into one Splunk dataSource.
 *
 * Series do not have to share timestamps: the per-worker chart has one series
 * per slot, and a slot that started late has fewer. So the x column is the
 * union of every series' timestamps, and a series with no value at one of them
 * gets a null, which `nullValueDisplay: 'gaps'` draws as a break in the line
 * rather than a straight line across it. A worker that went quiet has to look
 * like a hole, not like steady throughput.
 */
function toDataSource(series) {
    const xs = new Set();
    series.forEach((s) => (s.points || []).forEach(([x]) => xs.add(x)));
    const times = Array.from(xs).sort((a, b) => a - b);
    const index = new Map(times.map((t, i) => [t, i]));

    const columns = [times.map(iso)];
    series.forEach((s) => {
        const column = new Array(times.length).fill(null);
        (s.points || []).forEach(([x, y]) => {
            if (y !== null && y !== undefined && !Number.isNaN(y)) {
                column[index.get(x)] = String(y);
            }
        });
        columns.push(column);
    });

    return {
        data: {
            fields: [{ name: '_time' }, ...series.map((s) => ({ name: s.label }))],
            columns,
        },
        meta: { totalCount: times.length },
    };
}

/*
 * Evictions, stops and lost workers, as the chart's own annotations.
 *
 * annotationX/Label/Color take plain arrays, and that is what is used here.
 * Splunk's own examples feed them from a second dataSource through the
 * dynamic-options DSL ("> annotation|seriesByIndex(0)"), which is how a Studio
 * dashboard binds a search to them. That form was tried first and drew
 * nothing: the annotation layer was created and left empty, with no error
 * anywhere. Outside a dashboard the indirection buys nothing, so the values go
 * in directly.
 *
 * Each becomes a dashed vertical rule in the marker's colour with the label on
 * hover, which is Splunk's own presentation of an annotation. The chart drops
 * any annotation outside the x range, which is the behaviour we want: a marker
 * from before the first sample is not a marker.
 */
function toAnnotations(markers, theme) {
    if (!markers || !markers.length) {
        return {};
    }
    const colour = (kind) =>
        kind === 'evict'
            ? theme.warningColor
            : kind === 'lost' || kind === 'stop'
              ? theme.errorColor
              : theme.contentColorMuted;
    return {
        annotationX: markers.map((m) => iso(m.at)),
        annotationLabel: markers.map((m) => m.label || ''),
        annotationColor: markers.map((m) => colour(m.kind)),
    };
}

export default function SplunkLine({ series, markers, leftTitle, rightTitle, width, height }) {
    const theme = useSplunkTheme();

    const { dataSources, options } = useMemo(() => {
        const overlay = series.filter((s) => s.axis === 'right').map((s) => s.label);
        const colours = {};
        const dashes = {};
        series.forEach((s) => {
            if (s.color) {
                colours[s.label] = s.color;
            }
            if (s.dashed) {
                dashes[s.label] = 'shortDash';
            }
        });

        const annotations = toAnnotations(markers, theme);

        return {
            dataSources: { primary: toDataSource(series) },
            options: {
                nullValueDisplay: 'gaps',
                legendDisplay: 'bottom',
                markerDisplay: 'off',
                showYMajorGridLines: true,
                // The card behind the chart is already a themed surface, so
                // the chart must not paint its own over the top of it.
                backgroundColor: 'transparent',
                ...(overlay.length ? { overlayFields: overlay, showOverlayY2Axis: true } : {}),
                ...(Object.keys(colours).length ? { seriesColorsByField: colours } : {}),
                ...(Object.keys(dashes).length ? { lineDashStylesByField: dashes } : {}),
                ...(leftTitle ? { yAxisTitleText: leftTitle } : {}),
                ...(rightTitle ? { y2AxisTitleText: rightTitle } : {}),
                ...annotations,
            },
        };
    }, [series, markers, leftTitle, rightTitle, theme]);

    return <Line width={width} height={height} options={options} dataSources={dataSources} />;
}

SplunkLine.propTypes = {
    series: PropTypes.array.isRequired,
    markers: PropTypes.array,
    leftTitle: PropTypes.string,
    rightTitle: PropTypes.string,
    width: PropTypes.number.isRequired,
    height: PropTypes.number.isRequired,
};
