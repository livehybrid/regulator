/*
 * The target report: everything Regulator could learn about an instance
 * before deciding whether it is worth pointing load at.
 */
import React from 'react';
import PropTypes from 'prop-types';
import Chip from '@splunk/react-ui/Chip';
import DL from '@splunk/react-ui/DefinitionList';
import List from '@splunk/react-ui/List';
import Message from '@splunk/react-ui/Message';
import P from '@splunk/react-ui/Paragraph';
import Table from '@splunk/react-ui/Table';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';

import BusyButton from '../components/BusyButton';
import CacheView, { CacheLine } from '../components/CacheView';
import Dialog from '../components/Dialog';
import { Mono, Muted, SectionHeading } from '../components/primitives';
import { copyText } from '../clipboard';
import { fmtBytes, fmtInt, fmtNum, isBlank } from '../format';
import { useNotify } from '../Notifications';

export default function ReportModal({ report, loading, onClose }) {
    const notify = useNotify();
    const r = report || {};
    const inst = r.instance || {};
    const conc = r.concurrency || {};

    const copy = async () => {
        const ok = await copyText(JSON.stringify(r, null, 2));
        notify(
            ok ? 'Report JSON copied to the clipboard' : 'The clipboard refused; the JSON is in the browser console',
            ok ? 'success' : 'warning'
        );
    };

    return (
        <Dialog
            title="Target report"
            width="1080px"
            onClose={onClose}
            footer={!loading && <BusyButton appearance="primary" label="Copy JSON" onClick={copy} />}
        >
            {loading ? (
                <P>
                    <WaitSpinner /> Reading the instance. A full report dispatches several searches
                    and can take the best part of a minute.
                </P>
            ) : (
                <>
                    {r.reachable === false && (
                        <Message appearance="fill" type="error">
                            <Message.Title>Unreachable</Message.Title>
                            Regulator could not talk to this target, so everything below is
                            whatever it managed to read.
                        </Message>
                    )}

                    <DL termWidth="180px">
                        <DL.Term>Target</DL.Term>
                        <DL.Description>{r.target_url || 'unknown'}</DL.Description>
                        <DL.Term>Auth</DL.Term>
                        <DL.Description>{r.auth_method || 'unknown'}</DL.Description>
                        <DL.Term>Instance</DL.Term>
                        <DL.Description>
                            {inst.server_name || 'unknown'}, Splunk {inst.version || 'unknown'}{' '}
                            (build {inst.build || '?'}) on {inst.os_name || 'unknown'}
                        </DL.Description>
                        <DL.Term>Roles</DL.Term>
                        <DL.Description>{(inst.roles || []).join(', ') || 'unknown'}</DL.Description>
                        <DL.Term>Hardware</DL.Term>
                        <DL.Description>
                            {inst.cores === undefined ? '?' : inst.cores} cores,{' '}
                            {isBlank(inst.physical_memory_mb)
                                ? '? memory'
                                : fmtBytes(inst.physical_memory_mb * 1024 * 1024)}
                        </DL.Description>
                        <DL.Term>Concurrency</DL.Term>
                        <DL.Description>
                            base {conc.base_max_searches} + {conc.max_searches_per_cpu}/cpu ={' '}
                            <strong>{conc.max_hist_searches}</strong> concurrent historical
                            searches
                        </DL.Description>
                        <DL.Term>Can dispatch</DL.Term>
                        <DL.Description>
                            <Chip appearance={r.can_dispatch === false ? 'error' : 'success'}>
                                {r.can_dispatch === false ? 'no' : 'yes'}
                            </Chip>
                        </DL.Description>
                        <DL.Term>Suggested scenario</DL.Term>
                        <DL.Description>{r.recommended_scenario || 'none suggested'}</DL.Description>
                    </DL>

                    {(r.notes || []).length > 0 && (
                        <>
                            <SectionHeading>Notes</SectionHeading>
                            <Message appearance="fill" type="info">
                                <List>
                                    {r.notes.map((n) => (
                                        <List.Item key={n}>{n}</List.Item>
                                    ))}
                                </List>
                            </Message>
                        </>
                    )}

                    <SectionHeading>SmartStore cache</SectionHeading>
                    <P>
                        <CacheLine cache={r.smartstore_cache} />
                    </P>
                    {r.smartstore_cache && r.smartstore_cache.available !== false && (
                        <CacheView cache={r.smartstore_cache} />
                    )}

                    <SectionHeading>Search peers</SectionHeading>
                    {(r.search_peers || []).length ? (
                        <Table>
                            <Table.Head>
                                <Table.HeadCell>Peer</Table.HeadCell>
                                <Table.HeadCell>Status</Table.HeadCell>
                                <Table.HeadCell>Version</Table.HeadCell>
                            </Table.Head>
                            <Table.Body>
                                {r.search_peers.map((p) => (
                                    <Table.Row key={p.name}>
                                        <Table.Cell>
                                            <Mono>{p.name}</Mono>
                                        </Table.Cell>
                                        <Table.Cell>{p.status || ''}</Table.Cell>
                                        <Table.Cell>
                                            <Mono>{p.version || ''}</Mono>
                                        </Table.Cell>
                                    </Table.Row>
                                ))}
                            </Table.Body>
                        </Table>
                    ) : (
                        <Muted>None reported (this looks like a standalone instance).</Muted>
                    )}

                    <SectionHeading>Indexes with data</SectionHeading>
                    {(r.indexes || []).length ? (
                        <Table horizontalOverflow="scroll">
                            <Table.Head>
                                <Table.HeadCell>Index</Table.HeadCell>
                                <Table.HeadCell>Type</Table.HeadCell>
                                <Table.HeadCell align="right">Events</Table.HeadCell>
                                <Table.HeadCell align="right">Size (MB)</Table.HeadCell>
                                <Table.HeadCell> </Table.HeadCell>
                            </Table.Head>
                            <Table.Body>
                                {r.indexes.map((i) => (
                                    <Table.Row key={i.name}>
                                        <Table.Cell>
                                            <Mono>{i.name}</Mono>
                                        </Table.Cell>
                                        <Table.Cell>{i.datatype || ''}</Table.Cell>
                                        <Table.Cell align="right">{fmtInt(i.events)}</Table.Cell>
                                        <Table.Cell align="right">{fmtNum(i.size_mb, 0)}</Table.Cell>
                                        <Table.Cell>
                                            {i.smartstore && <Chip appearance="info">s2</Chip>}{' '}
                                            {i.internal && <Chip appearance="outline">internal</Chip>}
                                        </Table.Cell>
                                    </Table.Row>
                                ))}
                            </Table.Body>
                        </Table>
                    ) : (
                        <Muted>No indexes reported.</Muted>
                    )}

                    <SectionHeading>Sourcetype census (last 7 days)</SectionHeading>
                    {(r.sourcetypes || []).length ? (
                        <Table>
                            <Table.Head>
                                <Table.HeadCell>Index</Table.HeadCell>
                                <Table.HeadCell>Sourcetype</Table.HeadCell>
                                <Table.HeadCell align="right">Events</Table.HeadCell>
                            </Table.Head>
                            <Table.Body>
                                {r.sourcetypes.map((s) => (
                                    <Table.Row key={`${s.index}:${s.sourcetype}`}>
                                        <Table.Cell>
                                            <Mono>{s.index}</Mono>
                                        </Table.Cell>
                                        <Table.Cell>
                                            <Mono>{s.sourcetype}</Mono>
                                        </Table.Cell>
                                        <Table.Cell align="right">{fmtInt(s.events_7d)}</Table.Cell>
                                    </Table.Row>
                                ))}
                            </Table.Body>
                        </Table>
                    ) : (
                        <Muted>No sourcetypes reported.</Muted>
                    )}
                </>
            )}
        </Dialog>
    );
}

ReportModal.propTypes = {
    report: PropTypes.object,
    loading: PropTypes.bool,
    onClose: PropTypes.func.isRequired,
};
