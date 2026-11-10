/*
 * The pieces of a warm-up plan the Warm cache dialog and the Launch dialog
 * share: the forecast summary, the editable table of lines, and the rules for
 * turning what was typed into what is sent.
 *
 * One table for both, because the contract is the same in both places: the
 * tool may fill the table with a suggestion, the operator edits it, and the
 * lines as they left them are what gets warmed.
 */
import React from 'react';
import PropTypes from 'prop-types';
import Button from '@splunk/react-ui/Button';
import DL from '@splunk/react-ui/DefinitionList';
import Table from '@splunk/react-ui/Table';
import Text from '@splunk/react-ui/Text';

import { Actions, Mono, Muted, Note } from './primitives';
import { fmtBytes, fmtInt, fmtPct } from '../format';

export const emptyRow = () => ({ index: '', earliest: '-24h', latest: 'now' });

/** A suggestion's lines as table rows, with the times as readable ISO. */
export const rowsFromSuggestion = (plan) =>
    (plan || []).map((line) => ({
        index: line.index,
        earliest: line.earliest_iso,
        latest: line.latest_iso,
    }));

/** A plan as it was stored on a run: the lines exactly as they were written. */
export const rowsFromStored = (plan) =>
    (plan || []).map((line) => ({
        index: String(line.index || ''),
        earliest: String(line.earliest === undefined || line.earliest === null ? '' : line.earliest),
        latest: String(line.latest === undefined || line.latest === null ? 'now' : line.latest),
    }));

/** Every line names an index and an earliest time. */
export const planValid = (rows) =>
    rows.length > 0 && rows.every((r) => r.index.trim() && String(r.earliest).trim());

/** The rows as the API takes them. */
export const planLines = (rows) =>
    rows.map((r) => ({
        index: r.index.trim(),
        earliest: String(r.earliest).trim(),
        latest: String(r.latest).trim() || 'now',
    }));

export function ForecastSummary({ forecast, label }) {
    if (!forecast) {
        return null;
    }
    if (!forecast.available) {
        return <Note>{label}: the cache could not be read ({forecast.reason}).</Note>;
    }
    const residency = forecast.residency && forecast.residency.available ? forecast.residency : null;
    const counts = (residency && residency.counts) || {};
    return (
        <DL termWidth="200px">
            <DL.Term>{label}</DL.Term>
            <DL.Description>
                {fmtInt(forecast.buckets)} buckets, {fmtBytes(forecast.bytes_total)};{' '}
                {fmtInt(forecast.local)} local ({fmtPct(forecast.local_pct, 0)})
            </DL.Description>
            {residency && (
                <>
                    <DL.Term>On disk (dbinspect)</DL.Term>
                    <DL.Description>
                        {fmtInt(counts.full)} full, {fmtInt(counts.index_only)} index files only,{' '}
                        {fmtInt(counts.partial)} partial, {fmtInt(counts.stub)} evicted;{' '}
                        {fmtBytes(residency.bytes_outstanding)} not yet local
                    </DL.Description>
                </>
            )}
            {forecast.capacity_pct !== null && forecast.capacity_pct !== undefined && (
                <>
                    <DL.Term>Share of the cache</DL.Term>
                    <DL.Description>{fmtPct(forecast.capacity_pct, 0)}</DL.Description>
                </>
            )}
        </DL>
    );
}

ForecastSummary.propTypes = {
    forecast: PropTypes.object,
    label: PropTypes.string.isRequired,
};

/** What a suggestion could not be sure of: the notes on its forecast. */
export function SuggestionNotes({ suggestion }) {
    const notes = (suggestion && (suggestion.forecast || {}).notes) || [];
    if (!notes.length) {
        return null;
    }
    return (
        <Note>
            <strong>What the suggestion could not be sure of:</strong>
            <ul>
                {notes.map((n) => (
                    <li key={n}>{n}</li>
                ))}
            </ul>
        </Note>
    );
}

SuggestionNotes.propTypes = { suggestion: PropTypes.object };

/**
 * The editable plan. `onChange` gets the whole next list of rows; the caller
 * withdraws its confirmation on every call, because what was checked is what
 * gets warmed and not something edited afterwards.
 */
export function WarmPlanTable({ rows, onChange }) {
    const setCell = (i, key) => (e, { value }) =>
        onChange(rows.map((row, n) => (n === i ? { ...row, [key]: value } : row)));
    return (
        <>
            <Muted $small>
                Times take ISO 8601 (UTC unless a zone is given), an epoch, or a Splunk modifier
                such as <Mono>-7d@d</Mono>. Modifiers are resolved when the warm-up starts.
            </Muted>
            <Table>
                <Table.Head>
                    <Table.HeadCell>Index</Table.HeadCell>
                    <Table.HeadCell>Earliest</Table.HeadCell>
                    <Table.HeadCell>Latest</Table.HeadCell>
                    <Table.HeadCell />
                </Table.Head>
                <Table.Body>
                    {rows.map((row, i) => (
                        // Rows have no identity beyond their position while
                        // being edited, and are never reordered.
                        // eslint-disable-next-line react/no-array-index-key
                        <Table.Row key={i}>
                            <Table.Cell>
                                <Text value={row.index} placeholder="main" onChange={setCell(i, 'index')} />
                            </Table.Cell>
                            <Table.Cell>
                                <Text value={row.earliest} onChange={setCell(i, 'earliest')} />
                            </Table.Cell>
                            <Table.Cell>
                                <Text value={row.latest} onChange={setCell(i, 'latest')} />
                            </Table.Cell>
                            <Table.Cell>
                                <Button
                                    appearance="subtle"
                                    label="Remove"
                                    onClick={() => onChange(rows.filter((_, n) => n !== i))}
                                />
                            </Table.Cell>
                        </Table.Row>
                    ))}
                </Table.Body>
            </Table>
            <Actions>
                <Button label="Add a line" onClick={() => onChange([...rows, emptyRow()])} />
            </Actions>
        </>
    );
}

WarmPlanTable.propTypes = {
    rows: PropTypes.arrayOf(
        PropTypes.shape({
            index: PropTypes.string,
            earliest: PropTypes.oneOfType([PropTypes.string, PropTypes.number]),
            latest: PropTypes.oneOfType([PropTypes.string, PropTypes.number]),
        })
    ).isRequired,
    onChange: PropTypes.func.isRequired,
};
