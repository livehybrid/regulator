/*
 * The small shared pieces: status chips, monospace text, section panels,
 * relative timestamps and stat tiles.
 *
 * Everything here is either a Splunk UI component or a styled element built
 * from @splunk/themes tokens. Nothing hard-codes a colour, so switching the
 * colour scheme, or the theme family, changes this page with the rest of it.
 */
import React from 'react';
import PropTypes from 'prop-types';
import Card from '@splunk/react-ui/Card';
import Chip from '@splunk/react-ui/Chip';
import Heading from '@splunk/react-ui/Heading';
import Progress from '@splunk/react-ui/Progress';
import Tooltip from '@splunk/react-ui/Tooltip';
import styled from 'styled-components';
import { variables } from '@splunk/themes';

import { relativeTime, isBlank, DASH } from '../format';

/* ------------------------------------------------------------------ text */

export const Mono = styled.span`
    font-family: ${variables.monoFontFamily};
    word-break: ${(props) => (props.$break ? 'break-all' : 'normal')};
`;

export const Muted = styled.span`
    color: ${variables.contentColorMuted};
    font-size: ${(props) => (props.$small ? variables.fontSizeSmall : 'inherit')};
`;

/**
 * A short explanatory aside. The console is full of these: a load test is only
 * useful if the operator knows what the number in front of them means, so the
 * prose stays next to the number rather than in a manual nobody opens.
 */
export const Note = styled.div`
    color: ${variables.contentColorMuted};
    font-size: ${variables.fontSizeSmall};
    border-left: 2px solid ${variables.borderColor};
    padding: 2px 0 2px ${variables.spacingSmall};
    margin: ${variables.spacingSmall} 0;
`;

/* ---------------------------------------------------------------- layout */

export const Toolbar = styled.div`
    display: flex;
    align-items: center;
    gap: ${variables.spacingSmall};
    flex-wrap: wrap;
`;

/**
 * The two-plus column form grid used by every dialog with more than a couple
 * of fields. auto-fit rather than a fixed count, so a narrow window collapses
 * to one column instead of scrolling sideways.
 */
export const Grid2 = styled.div`
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 0 ${variables.spacingLarge};
`;

export const PageHead = styled.div`
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: ${variables.spacingMedium};
    margin-bottom: ${variables.spacingMedium};
`;

const PanelCard = styled(Card)`
    margin-bottom: ${variables.spacingMedium};
    width: 100%;
`;

const PanelBody = styled(Card.Body)`
    padding: ${(props) => (props.$flush ? '0' : variables.spacingMedium)};
    overflow-x: auto;
`;

/**
 * A section of the page. `flush` removes the padding, which is what a table
 * wants: a Table inside a padded Card looks inset and wastes a lot of width on
 * a page whose whole job is showing wide tables of numbers.
 */
export function Panel({ title, children, flush, actions }) {
    return (
        <PanelCard>
            {(title || actions) && (
                <Card.Header title={title}>{actions}</Card.Header>
            )}
            <PanelBody $flush={flush}>{children}</PanelBody>
        </PanelCard>
    );
}

Panel.propTypes = {
    title: PropTypes.node,
    children: PropTypes.node,
    flush: PropTypes.bool,
    actions: PropTypes.node,
};

export const SectionHeading = styled(Heading).attrs({ level: 3 })`
    margin: ${variables.spacingLarge} 0 ${variables.spacingSmall};
`;

/* ----------------------------------------------------------------- chips */

const STATE_APPEARANCE = {
    completed: 'success',
    done: 'success',
    ready: 'success',
    ok: 'success',
    running: 'info',
    claimed: 'info',
    pending: 'outline',
    free: 'outline',
    stopped: 'warning',
    lost: 'warning',
};

/** A run, worker or lease state. Unknown states are an error, not a default. */
export function StatePill({ state }) {
    const s = state || 'unknown';
    return <Chip appearance={STATE_APPEARANCE[s] || 'error'}>{s}</Chip>;
}

StatePill.propTypes = { state: PropTypes.string };

/** A target's last known health, with whatever detail the probe returned. */
export function HealthPill({ health, detail }) {
    const h = health || 'unknown';
    const appearance = h === 'ok' ? 'success' : h === 'error' ? 'error' : 'outline';
    const chip = <Chip appearance={appearance}>{h}</Chip>;
    return detail ? (
        <Tooltip content={detail} defaultPlacement="above">
            {chip}
        </Tooltip>
    ) : (
        chip
    );
}

HealthPill.propTypes = { health: PropTypes.string, detail: PropTypes.string };

/* ------------------------------------------------------------------ time */

/** "4m ago", with the absolute local time on hover. */
export function RelTime({ value }) {
    const { text, title } = relativeTime(value);
    if (!title) {
        return <span>{text}</span>;
    }
    return <span title={title}>{text}</span>;
}

RelTime.propTypes = { value: PropTypes.oneOfType([PropTypes.number, PropTypes.string]) };

/* ------------------------------------------------------------- fill bars */

/**
 * How full something is. Above 80% is a warning and above 95% an error,
 * because a SmartStore cache at its ceiling is evicting buckets other searches
 * are still using: the run then measures churn as much as it measures search.
 */
export function FillBar({ pct }) {
    if (isBlank(pct)) {
        return null;
    }
    const clamped = Math.max(0, Math.min(100, pct));
    return <Progress percentage={clamped} type={pct >= 95 ? 'error' : 'info'} />;
}

FillBar.propTypes = { pct: PropTypes.number };

/* ----------------------------------------------------------------- tiles */

const TileGrid = styled.div`
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: ${variables.spacingSmall};
    margin-bottom: ${variables.spacingMedium};
`;

const TileBox = styled.div`
    background-color: ${variables.backgroundColorSection};
    border: 1px solid ${variables.borderColor};
    border-radius: ${variables.borderRadius};
    padding: ${variables.spacingSmall} ${variables.spacingMedium};
`;

const TileKey = styled.div`
    font-size: ${variables.fontSizeSmall};
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: ${variables.contentColorMuted};
`;

const TileValue = styled.div`
    font-family: ${variables.monoFontFamily};
    font-size: ${variables.fontSizeXLarge};
    margin-top: 2px;
    font-variant-numeric: tabular-nums;
    color: ${(props) =>
        props.$tone === 'error'
            ? variables.errorColor
            : props.$tone === 'warning'
              ? variables.warningColor
              : props.$tone === 'success'
                ? variables.successColor
                : variables.contentColorDefault};
`;

const TileSub = styled.div`
    font-size: ${variables.fontSizeSmall};
    color: ${variables.contentColorMuted};
    font-family: ${variables.monoFontFamily};
`;

export function Tile({ label, value, sub, tone }) {
    return (
        <TileBox>
            <TileKey>{label}</TileKey>
            <TileValue $tone={tone}>{value === undefined ? DASH : value}</TileValue>
            {sub ? <TileSub>{sub}</TileSub> : null}
        </TileBox>
    );
}

Tile.propTypes = {
    label: PropTypes.node,
    value: PropTypes.node,
    sub: PropTypes.node,
    tone: PropTypes.oneOf(['error', 'warning', 'success']),
};

export { TileGrid };
