/*
 * The run page's charts, drawn from the samples the control plane stored.
 *
 * Each shows what happened in the interval, which is honest about a run that
 * degraded late, with the cumulative figure dashed on top because that is the
 * number a gate actually reads.
 *
 * The x value passed down is the sample's own timestamp, not an offset from
 * the start of the run: the charts are Splunk time-series charts, and a spike
 * here lining up with wall-clock time in the customer's own dashboards is
 * worth more than an axis that counts from zero.
 */
import React from 'react';
import PropTypes from 'prop-types';
import styled from 'styled-components';
import { useSplunkTheme, variables } from '@splunk/themes';

import LineChart, { PALETTE } from './LineChart';
import { Muted } from './primitives';
import { isBlank } from '../format';

const Grid = styled.div`
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
    gap: ${variables.spacingMedium};
    margin-bottom: ${variables.spacingMedium};
`;

export default function RunCharts({ run, live, series }) {
    const theme = useSplunkTheme();
    const { rows } = series;
    const stats = run.stats || {};
    const ceiling = (stats.target || {}).max_hist_searches;

    const pts = (list, f) =>
        list.map((s) => {
            const v = f(s);
            return [s.at, v === null || v === undefined ? null : v];
        });
    const markers = series.markers || [];
    const iv = (s) => s.interval || {};

    const empty = live
        ? 'Waiting for the first samples (one every few seconds).'
        : 'No samples were stored for this run.';

    const bySlot = {};
    series.slotRows.forEach((s) => {
        (bySlot[s.slot] || (bySlot[s.slot] = [])).push(s);
    });
    const slots = Object.keys(bySlot).sort((a, b) => a - b);
    const fleeted = run.fleet && run.fleet !== 'inprocess';

    // A flat reference line across the whole run. Two points is enough: the
    // chart interpolates, and a value at every sample would put the ceiling in
    // every tooltip for no benefit.
    const span = rows.length ? [rows[0].at, rows[rows.length - 1].at] : null;
    const ceilingSeries =
        !isBlank(ceiling) && span
            ? [
                  {
                      label: `ceiling ${ceiling}`,
                      color: theme.errorColor,
                      dashed: true,
                      axis: 'right',
                      points: span.map((at) => [at, ceiling]),
                  },
              ]
            : [];

    const lastRow = rows.length ? rows[rows.length - 1] : null;
    const percentilesNote =
        lastRow && lastRow.interval && lastRow.interval.percentiles_note ? (
            <Muted $small>
                Interval percentiles are the busiest worker&apos;s while the fleet is running; the
                merged figures arrive with the final reports.
            </Muted>
        ) : null;

    return (
        <>
            <Grid>
                <LineChart
                    title="Throughput and in flight"
                    leftTitle="searches/s"
                    rightTitle="in flight"
                    markers={markers}
                    empty={empty}
                    series={[
                        {
                            label: 'searches/s',
                            points: pts(rows, (s) => iv(s).throughput_per_s),
                        },
                        {
                            label: 'in flight',
                            axis: 'right',
                            points: pts(rows, (s) => s.in_flight),
                        },
                        ...ceilingSeries,
                    ]}
                />

                <LineChart
                    title="Search latency"
                    leftTitle="ms"
                    note="Total search time, per interval, with the cumulative p95 dashed. Time to first result (TTFR) is tracked separately in the step table: it is when the first row comes back, which is what a user feels."
                    markers={markers}
                    empty={empty}
                    series={[
                        { label: 'p50', points: pts(rows, (s) => iv(s).p50_ms) },
                        // p95 and the cumulative p95 are the same measurement
                        // at two horizons, so they share a colour and differ
                        // by dash rather than being two unrelated lines.
                        { label: 'p95', color: PALETTE[1], points: pts(rows, (s) => iv(s).p95_ms) },
                        { label: 'p99', points: pts(rows, (s) => iv(s).p99_ms) },
                        {
                            label: 'p95 so far',
                            color: PALETTE[1],
                            dashed: true,
                            points: pts(rows, (s) => (s.cum || {}).p95_ms),
                        },
                    ]}
                />

                <LineChart
                    title="Errors and queueing"
                    leftTitle="% of the interval"
                    rightTitle="queued p95 ms"
                    markers={markers}
                    empty={empty}
                    series={[
                        {
                            label: 'error %',
                            color: theme.errorColor,
                            points: pts(rows, (s) => iv(s).error_rate_pct),
                        },
                        {
                            label: 'queued %',
                            color: theme.warningColor,
                            points: pts(rows, (s) => iv(s).queued_pct),
                        },
                        {
                            label: 'queued p95 ms',
                            axis: 'right',
                            points: pts(rows, (s) => iv(s).queued_p95_ms),
                        },
                    ]}
                />

                {fleeted ? (
                    <LineChart
                        title="Per worker"
                        leftTitle="searches/s"
                        markers={markers}
                        empty={
                            live
                                ? "Waiting for the workers' first intervals."
                                : 'No per-worker samples were stored.'
                        }
                        series={slots.map((slot) => ({
                            label: `slot ${slot}`,
                            points: pts(bySlot[slot], (s) => iv(s).throughput_per_s),
                        }))}
                    />
                ) : (
                    <LineChart
                        title="Generator loop lag p95"
                        leftTitle="ms"
                        note="This process, not Splunk: if the generator itself is late, the latency it reports is partly its own."
                        markers={markers}
                        empty={empty}
                        series={[
                            {
                                label: 'loop lag p95',
                                points: pts(rows, (s) => iv(s).loop_lag_p95_ms),
                            },
                        ]}
                    />
                )}
            </Grid>
            {percentilesNote}
        </>
    );
}

RunCharts.propTypes = {
    run: PropTypes.object.isRequired,
    live: PropTypes.bool,
    series: PropTypes.object.isRequired,
};
