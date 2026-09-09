/*
 * The run charts, drawn by Splunk's own visualization library.
 *
 * These are @splunk/visualizations Line charts, the same component Dashboard
 * Studio renders, so the axes, gridlines, legend, tooltips, hover behaviour
 * and series palette are the ones an operator already knows from Splunk Web
 * rather than an approximation of them.
 *
 * This file holds the card, the sizing and the enlarge dialog. The mapping
 * onto Splunk's dataSource contract is in SplunkLine, which is loaded lazily
 * because the chart library is large and most of the console has no chart on
 * it. Everything upstream passes plain `[epochSeconds, value]` arrays, so the
 * pages never see that contract at all.
 *
 * Three things are worth knowing about the mapping:
 *
 *  - The x field is `_time`, in ISO 8601. That makes the axis a real time axis
 *    rather than 300 string categories, and it means a spike here lines up
 *    with wall-clock time in the customer's own dashboards, which is most of
 *    the point of running the test beside their Splunk.
 *  - A second y-axis is `overlayFields` + `showOverlayY2Axis`, which is how
 *    Studio does an overlay. "In flight against a ceiling" is the chart that
 *    needs it.
 *  - Evictions, stops and lost workers are a real `annotation` dataSource, so
 *    they are drawn and labelled by the library rather than by hand.
 *
 * Series colours are Splunk's default categorical palette unless a colour
 * carries meaning (a ceiling, an error rate), because a chart that invents its
 * own colours for ordinary series is exactly the inconsistency this replaced.
 */
import React, { Suspense, lazy, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import ArrowsFourCorners from '@splunk/react-icons/ArrowsFourCorners';
import Button from '@splunk/react-ui/Button';
import Card from '@splunk/react-ui/Card';
import Heading from '@splunk/react-ui/Heading';
import Modal from '@splunk/react-ui/Modal';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';
import styled from 'styled-components';
import useResizeObserver from '@splunk/react-ui/useResizeObserver';
import { VIZ_CATEGORICAL } from '@splunk/visualization-color-palettes';
import { useSplunkTheme, variables } from '@splunk/themes';

import { Muted } from './primitives';

const SplunkLine = lazy(() => import(/* webpackChunkName: "charts" */ './SplunkLine'));

/**
 * Splunk's default series palette, which is what the Line chart uses when it
 * is left to choose. Exposed so a page can pin one series to the same colour
 * as another (p95 and the cumulative p95 belong together, one dashed).
 */
export const PALETTE = VIZ_CATEGORICAL;

/* --------------------------------------------------------------- wrapper */

const ChartCard = styled(Card)`
    width: 100%;
    position: relative;
`;

const Zoom = styled.div`
    position: absolute;
    top: ${variables.spacingSmall};
    right: ${variables.spacingSmall};
    opacity: 0;
    transition: opacity 0.12s;

    ${ChartCard}:hover &,
    &:focus-within {
        opacity: 1;
    }
`;

const Measured = styled.div`
    width: 100%;
`;

const Loading = styled.div`
    display: flex;
    align-items: center;
    gap: ${variables.spacingSmall};
    color: ${variables.contentColorMuted};
    font-size: ${variables.fontSizeSmall};
`;

/**
 * A chart at the width of its container.
 *
 * The Splunk chart wants a pixel width rather than a percentage, so the
 * container is measured with react-ui's own resize hook and the chart is
 * re-rendered when it changes. Before the first measurement there is no width
 * to draw at; guessing one and correcting it makes the chart visibly jump, so
 * it waits a frame instead.
 */
function SizedChart({ height, ...rest }) {
    const box = useRef(null);
    const { width } = useResizeObserver(box);
    return (
        <Measured ref={box}>
            {width > 0 && (
                <Suspense
                    fallback={
                        <Loading>
                            <WaitSpinner /> Loading the chart library&hellip;
                        </Loading>
                    }
                >
                    <SplunkLine {...rest} width={width} height={height} />
                </Suspense>
            )}
        </Measured>
    );
}

SizedChart.propTypes = { height: PropTypes.number.isRequired };

/** One chart, with a title, an optional footnote and click-to-enlarge. */
export default function LineChart({
    title,
    note,
    empty,
    height = 220,
    series = [],
    markers,
    leftTitle,
    rightTitle,
}) {
    const [open, setOpen] = useState(false);
    const toggle = useRef(null);
    const drawable = series.some((s) => (s.points || []).length >= 2);
    const chartProps = { series, markers, leftTitle, rightTitle };

    return (
        <>
            <ChartCard>
                <Card.Body>
                    <Heading level={4}>{title}</Heading>
                    {drawable ? (
                        <SizedChart {...chartProps} height={height} />
                    ) : (
                        <Muted $small>{empty || 'Waiting for the first samples.'}</Muted>
                    )}
                    {note ? <Muted $small>{note}</Muted> : null}
                </Card.Body>
                {drawable && (
                    <Zoom>
                        <Button
                            appearance="subtle"
                            icon={<ArrowsFourCorners />}
                            label="Enlarge"
                            hideLabel
                            elementRef={toggle}
                            onClick={() => setOpen(true)}
                        />
                    </Zoom>
                )}
            </ChartCard>
            <Modal
                open={open}
                onRequestClose={() => setOpen(false)}
                returnFocus={toggle}
                closeOnClickAway
                style={{ width: 'min(1400px, 94vw)' }}
            >
                <Modal.Header title={title} onRequestClose={() => setOpen(false)} />
                <Modal.Body>
                    {open && <SizedChart {...chartProps} height={Math.round(height * 2.1)} />}
                    {note ? <Muted $small>{note}</Muted> : null}
                </Modal.Body>
            </Modal>
        </>
    );
}

LineChart.propTypes = {
    title: PropTypes.string,
    note: PropTypes.node,
    empty: PropTypes.string,
    height: PropTypes.number,
    series: PropTypes.array,
    markers: PropTypes.array,
    leftTitle: PropTypes.string,
    rightTitle: PropTypes.string,
};

/*
 * The p95-over-the-run column in the runs table.
 *
 * This one stays a hand-drawn SVG, and deliberately: it is a 120x26 glyph in a
 * table cell, one per row, and a chart library instance per row of a fifty-row
 * table is a real cost for something that is a table decoration rather than a
 * chart. It has no axes, no legend and no tooltip to be inconsistent about;
 * the only thing it borrows from the theme is its colour. It also means the
 * runs list does not pull the chart library down at all.
 */
export function Sparkline({ values }) {
    const theme = useSplunkTheme();
    const pts = (values || [])
        .map((v, i) => [i, v])
        .filter((p) => p[1] !== null && p[1] !== undefined);
    if (pts.length < 2) {
        return null;
    }
    const W = 120;
    const H = 26;
    const max = Math.max(...pts.map((p) => p[1]), 1);
    const n = (values || []).length - 1 || 1;
    const at = (p) => [(p[0] / n) * (W - 2) + 1, H - 2 - (p[1] / max) * (H - 4)];
    const poly = pts.map((p) => at(p).map((v) => v.toFixed(1)).join(',')).join(' ');
    const [lx, ly] = at(pts[pts.length - 1]);
    return (
        <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} role="img" aria-label="p95 over the run">
            <polyline fill="none" stroke={theme.contentColorAccent} strokeWidth="1.3" points={poly} />
            <circle cx={lx} cy={ly} r="2" fill={theme.contentColorAccent} />
        </svg>
    );
}

Sparkline.propTypes = { values: PropTypes.array };
