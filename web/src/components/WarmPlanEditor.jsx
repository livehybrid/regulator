/*
 * The warm-up plan: suggested by Regulator, decided by the operator.
 *
 * Shared by the Warm cache dialog and the Launch dialog, so the rule is the
 * same in both places: a suggestion fills an editable table of indexes and
 * time ranges, nothing can be warmed until the operator ticks that they have
 * checked it, and any edit clears the tick. What is warmed is the table as
 * they left it.
 *
 * The state lives with the caller (`value` / `onChange`), because the caller
 * is what sends it: {rows, mode, confirmed, suggestion}.
 */
import React from 'react';
import PropTypes from 'prop-types';
import Button from '@splunk/react-ui/Button';
import Checkbox from '@splunk/react-ui/Checkbox';
import ControlGroup from '@splunk/react-ui/ControlGroup';
import DL from '@splunk/react-ui/DefinitionList';
import Select from '@splunk/react-ui/Select';
import Table from '@splunk/react-ui/Table';
import Text from '@splunk/react-ui/Text';

import BusyButton from './BusyButton';
import { Actions, Grid2, Mono, Muted, Note } from './primitives';
import { api } from '../api';
import { fmtBytes, fmtInt, fmtPct } from '../format';
import { useNotify } from '../Notifications';

export const emptyPlan = () => ({ rows: [], mode: 'tsidx', confirmed: false, suggestion: null });

const emptyRow = () => ({ index: '', earliest: '-24h', latest: 'now' });

/** Whether every line names an index and a start. */
export const planIsValid = (plan) =>
    plan.rows.length > 0 && plan.rows.every((r) => r.index.trim() && String(r.earliest).trim());

/** The lines as the API wants them. */
export const planLines = (plan) =>
    plan.rows.map((r) => ({
        index: r.index.trim(),
        earliest: String(r.earliest).trim(),
        latest: String(r.latest).trim() || 'now',
    }));

export function ForecastSummary({ forecast, label }) {
    if (!forecast) {
        return null;
    }
    if (!forecast.available) {
        return (
            <Note>
                {label}: the cache could not be read ({forecast.reason}).
            </Note>
        );
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

export default function WarmPlanEditor({ targetId, scenario, durationS, value, onChange, disabled }) {
    const notify = useNotify();
    const { rows, mode, confirmed, suggestion } = value;

    // Any edit to the plan withdraws the confirmation: what was checked is
    // what gets warmed, not something edited afterwards.
    const edit = (next) => onChange({ ...value, ...next, confirmed: false });
    const setCell = (i, key) => (e, { value: v }) =>
        edit({ rows: rows.map((row, n) => (n === i ? { ...row, [key]: v } : row)) });

    const suggest = async () => {
        const body = { scenario, mode };
        if (durationS !== '' && durationS !== null && durationS !== undefined) {
            body.duration_s = Number(durationS);
        }
        const r = await api(`/targets/${encodeURIComponent(targetId)}/warm/suggest`, { method: 'POST', body });
        edit({
            suggestion: r,
            rows: (r.plan || []).map((line) => ({
                index: line.index,
                earliest: line.earliest_iso,
                latest: line.latest_iso,
            })),
        });
        if (!(r.plan || []).length) {
            notify('Nothing to suggest: what this scenario reads is already local, or not SmartStore', 'info');
        }
    };

    const valid = planIsValid(value);
    const notes = (suggestion && suggestion.forecast && suggestion.forecast.notes) || [];

    return (
        <>
            <Grid2>
                <ControlGroup
                    label="Mode"
                    labelPosition="top"
                    help="tsidx warms index files (cheap, enough for tstats); full also reads raw events."
                >
                    <Select value={mode} disabled={disabled} onChange={(e, { value: v }) => edit({ mode: v })}>
                        <Select.Option value="tsidx" label="tsidx: index files only" />
                        <Select.Option value="full" label="full: index files and raw data" />
                    </Select>
                </ControlGroup>
                <ControlGroup label="&nbsp;" labelPosition="top">
                    <BusyButton
                        label="Suggest a plan"
                        disabled={disabled || !scenario || !targetId}
                        onClick={suggest}
                    />
                </ControlGroup>
            </Grid2>
            {suggestion && <ForecastSummary forecast={suggestion.forecast} label="What the run reads" />}
            {notes.length > 0 && (
                <Note>
                    <strong>What the suggestion could not be sure of:</strong>
                    <ul>
                        {notes.map((n) => (
                            <li key={n}>{n}</li>
                        ))}
                    </ul>
                </Note>
            )}

            <Muted $small>
                Times take ISO 8601 (UTC unless a zone is given), an epoch, or a Splunk modifier such
                as <Mono>-7d@d</Mono>. Modifiers are resolved when the warm-up starts.
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
                                <Text value={row.index} placeholder="main" disabled={disabled} onChange={setCell(i, 'index')} />
                            </Table.Cell>
                            <Table.Cell>
                                <Text value={row.earliest} disabled={disabled} onChange={setCell(i, 'earliest')} />
                            </Table.Cell>
                            <Table.Cell>
                                <Text value={row.latest} disabled={disabled} onChange={setCell(i, 'latest')} />
                            </Table.Cell>
                            <Table.Cell>
                                <Button
                                    appearance="subtle"
                                    label="Remove"
                                    disabled={disabled}
                                    onClick={() => edit({ rows: rows.filter((_, n) => n !== i) })}
                                />
                            </Table.Cell>
                        </Table.Row>
                    ))}
                </Table.Body>
            </Table>
            <Actions>
                <Button label="Add a line" disabled={disabled} onClick={() => edit({ rows: [...rows, emptyRow()] })} />
            </Actions>

            <Checkbox
                checked={confirmed}
                disabled={disabled || !valid}
                onChange={() => onChange({ ...value, confirmed: !confirmed })}
                value="confirmed"
            >
                I have checked these indexes and time ranges, and want them warmed
            </Checkbox>
        </>
    );
}

WarmPlanEditor.propTypes = {
    targetId: PropTypes.any,
    scenario: PropTypes.string,
    durationS: PropTypes.oneOfType([PropTypes.string, PropTypes.number]),
    value: PropTypes.shape({
        rows: PropTypes.array.isRequired,
        mode: PropTypes.string.isRequired,
        confirmed: PropTypes.bool.isRequired,
        suggestion: PropTypes.object,
    }).isRequired,
    onChange: PropTypes.func.isRequired,
    disabled: PropTypes.bool,
};
