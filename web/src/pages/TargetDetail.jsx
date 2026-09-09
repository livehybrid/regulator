/*
 * One target's SmartStore cache over time: every reading the control plane
 * took, and why it took it. An eviction shows as a marker on the chart, so a
 * run that reads cold is visible rather than inferred.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import Button from '@splunk/react-ui/Button';
import Chip from '@splunk/react-ui/Chip';
import Heading from '@splunk/react-ui/Heading';
import Link from '@splunk/react-ui/Link';
import P from '@splunk/react-ui/Paragraph';
import Table from '@splunk/react-ui/Table';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';

import CacheModal from '../modals/CacheModal';
import LineChart from '../components/LineChart';
import PurgeModal from '../modals/PurgeModal';
import useLoader from '../useLoader';
import { Mono, Muted, PageHead, Panel, RelTime, Toolbar } from '../components/primitives';
import { api } from '../api';
import { fmtBytes, fmtInt, fmtPct } from '../format';

export default function TargetDetail({ targetId, navigate }) {
    const { data, error, loading, reload } = useLoader(
        () =>
            Promise.all([
                api(`/targets/${encodeURIComponent(targetId)}/samples`),
                api('/targets'),
            ]).then(([doc, targets]) => ({ doc, targets })),
        [targetId]
    );
    const [dialog, setDialog] = useState(null);

    const samples = (data && data.doc && data.doc.samples) || [];
    const target =
        (data && (data.targets || []).find((x) => String(x.id) === String(targetId))) || {
            name: `target ${targetId}`,
        };

    const openWith = async (kind) => {
        setDialog({ kind, loading: true, payload: null });
        try {
            const payload = await api(`/targets/${encodeURIComponent(targetId)}/cache`);
            setDialog((d) => (d && d.kind === kind ? { ...d, loading: false, payload } : d));
        } catch (e) {
            setDialog((d) => (d && d.kind === kind ? { ...d, loading: false, payload: null } : d));
        }
    };

    if (loading && !data) {
        return (
            <P>
                <WaitSpinner /> Loading target&hellip;
            </P>
        );
    }
    if (error) {
        return (
            <Panel>
                <Muted>{error}</Muted>
            </Panel>
        );
    }

    const chart = samples.length ? (
        <LineChart
            title="SmartStore cache"
            leftTitle="%"
            markers={samples
                .filter((s) => s.kind === 'evict')
                .map((s) => ({ at: s.at, label: 'evict', kind: 'evict' }))}
            series={[
                { label: 'local %', points: samples.map((s) => [s.at, s.local_pct]) },
                { label: 'fill %', points: samples.map((s) => [s.at, s.fill_pct]) },
            ]}
        />
    ) : (
        <Panel>
            <Muted>
                No cache readings yet. The cache dialog, an eviction and every run each take one.
            </Muted>
        </Panel>
    );

    return (
        <div>
            <PageHead>
                <div>
                    <Heading level={1}>{target.name}</Heading>
                    <Muted $small>
                        <Mono>{target.mgmt_url || ''}</Mono>
                    </Muted>
                </div>
                <Toolbar>
                    <Button label="All targets" onClick={() => navigate('targets')} />
                    <Button label="Read the cache now" onClick={() => openWith('cache')} />
                    <Button
                        appearance="destructive"
                        label="Purge cache"
                        onClick={() => openWith('purge')}
                    />
                </Toolbar>
            </PageHead>

            {chart}

            {samples.length > 0 && (
                <Panel flush>
                    <Table horizontalOverflow="scroll">
                        <Table.Head>
                            <Table.HeadCell>When</Table.HeadCell>
                            <Table.HeadCell>Reading</Table.HeadCell>
                            <Table.HeadCell align="right">Local / total buckets</Table.HeadCell>
                            <Table.HeadCell align="right">Local</Table.HeadCell>
                            <Table.HeadCell align="right">Fill</Table.HeadCell>
                            <Table.HeadCell align="right">Local bytes</Table.HeadCell>
                            <Table.HeadCell>Run</Table.HeadCell>
                        </Table.Head>
                        <Table.Body>
                            {samples
                                .slice()
                                .reverse()
                                .slice(0, 200)
                                .map((s, i) => (
                                    // Two readings can share a timestamp. The
                                    // samples are append-only and never
                                    // reordered, so the position is stable.
                                    // eslint-disable-next-line react/no-array-index-key
                                    <Table.Row key={`${s.at}-${i}`}>
                                        <Table.Cell>
                                            <RelTime value={s.at} />
                                        </Table.Cell>
                                        <Table.Cell>
                                            <Chip appearance="outline">{s.kind}</Chip>
                                        </Table.Cell>
                                        <Table.Cell align="right">
                                            {fmtInt(s.local_buckets)} / {fmtInt(s.total_buckets)}
                                        </Table.Cell>
                                        <Table.Cell align="right">{fmtPct(s.local_pct)}</Table.Cell>
                                        <Table.Cell align="right">{fmtPct(s.fill_pct)}</Table.Cell>
                                        <Table.Cell align="right">
                                            {fmtBytes(s.local_bytes)}
                                        </Table.Cell>
                                        <Table.Cell>
                                            {s.run_id ? (
                                                <Link
                                                    onClick={() =>
                                                        navigate(
                                                            `run/${encodeURIComponent(s.run_id)}`
                                                        )
                                                    }
                                                >
                                                    run {s.run_id}
                                                </Link>
                                            ) : null}
                                        </Table.Cell>
                                    </Table.Row>
                                ))}
                        </Table.Body>
                    </Table>
                </Panel>
            )}

            {dialog && dialog.kind === 'cache' && (
                <CacheModal
                    cache={dialog.payload}
                    loading={dialog.loading}
                    onClose={() => setDialog(null)}
                />
            )}
            {dialog && dialog.kind === 'purge' && (
                <PurgeModal
                    targetId={targetId}
                    cache={dialog.payload}
                    loading={dialog.loading}
                    onClose={() => setDialog(null)}
                    onDone={reload}
                />
            )}
        </div>
    );
}

TargetDetail.propTypes = {
    targetId: PropTypes.string.isRequired,
    navigate: PropTypes.func.isRequired,
};
