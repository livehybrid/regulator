import React from 'react';
import Heading from '@splunk/react-ui/Heading';
import P from '@splunk/react-ui/Paragraph';
import Table from '@splunk/react-ui/Table';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';

import useLoader from '../useLoader';
import { Mono, Muted, Note, PageHead, Panel, RelTime } from '../components/primitives';
import { api } from '../api';
import { isBlank } from '../format';

export default function Audit() {
    const { data, error, loading } = useLoader(() => api('/audit'), []);
    const events = (data && data.events) || [];

    return (
        <div>
            <PageHead>
                <Heading level={1}>Audit</Heading>
            </PageHead>
            <Note>
                Who did what: logins, launches, stops, evictions and scenario changes, with the
                caller&apos;s address and how they authenticated.
            </Note>

            {loading && !data ? (
                <P>
                    <WaitSpinner /> Loading&hellip;
                </P>
            ) : error ? (
                <Panel>
                    <Muted>{error}</Muted>
                </Panel>
            ) : !events.length ? (
                <Panel>
                    <Muted>Nothing recorded yet.</Muted>
                </Panel>
            ) : (
                <Panel flush>
                    <Table horizontalOverflow="scroll">
                        <Table.Head>
                            <Table.HeadCell>When</Table.HeadCell>
                            <Table.HeadCell>Action</Table.HeadCell>
                            <Table.HeadCell>Actor</Table.HeadCell>
                            <Table.HeadCell>From</Table.HeadCell>
                            <Table.HeadCell>Target</Table.HeadCell>
                            <Table.HeadCell>Detail</Table.HeadCell>
                        </Table.Head>
                        <Table.Body>
                            {events.map((e, i) => (
                                // Two audit events can share a timestamp, an
                                // action and an actor, so their position in
                                // the returned page is what distinguishes them.
                                // eslint-disable-next-line react/no-array-index-key
                                <Table.Row key={`${e.at}-${i}`}>
                                    <Table.Cell>
                                        <RelTime value={e.at} />
                                    </Table.Cell>
                                    <Table.Cell>
                                        <Mono>{e.action}</Mono>
                                    </Table.Cell>
                                    <Table.Cell>{e.actor}</Table.Cell>
                                    <Table.Cell>
                                        <Mono>{e.client || ''}</Mono>
                                    </Table.Cell>
                                    <Table.Cell>
                                        {isBlank(e.target_id) ? '' : `target ${e.target_id}`}
                                    </Table.Cell>
                                    <Table.Cell>{e.detail || ''}</Table.Cell>
                                </Table.Row>
                            ))}
                        </Table.Body>
                    </Table>
                </Panel>
            )}
        </div>
    );
}
