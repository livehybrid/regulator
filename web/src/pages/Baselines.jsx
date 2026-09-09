import React, { useState } from 'react';
import PropTypes from 'prop-types';
import Heading from '@splunk/react-ui/Heading';
import Link from '@splunk/react-ui/Link';
import Menu from '@splunk/react-ui/Menu';
import P from '@splunk/react-ui/Paragraph';
import Table from '@splunk/react-ui/Table';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';

import ConfirmModal from '../modals/ConfirmModal';
import useLoader from '../useLoader';
import { Mono, Muted, Note, PageHead, Panel, RelTime } from '../components/primitives';
import { api } from '../api';
import { useNotify } from '../Notifications';

export default function Baselines({ navigate }) {
    const { data, error, loading, reload } = useLoader(() => api('/baselines'), []);
    const baselines = data || [];
    const [deleting, setDeleting] = useState(null);
    const notify = useNotify();

    return (
        <div>
            <PageHead>
                <Heading level={1}>Baselines</Heading>
            </PageHead>
            <Note>
                A label a pipeline compares against. Promote a completed run from its detail page.
            </Note>

            {loading && !data ? (
                <P>
                    <WaitSpinner /> Loading&hellip;
                </P>
            ) : error ? (
                <Panel>
                    <Muted>{error}</Muted>
                </Panel>
            ) : !baselines.length ? (
                <Panel>
                    <Muted>No baselines yet.</Muted>
                </Panel>
            ) : (
                <Panel flush>
                    <Table>
                        <Table.Head>
                            <Table.HeadCell>Label</Table.HeadCell>
                            <Table.HeadCell>Run</Table.HeadCell>
                            <Table.HeadCell>Scenario</Table.HeadCell>
                            <Table.HeadCell>Made</Table.HeadCell>
                            <Table.HeadCell>Note</Table.HeadCell>
                        </Table.Head>
                        <Table.Body>
                            {baselines.map((b) => (
                                <Table.Row
                                    key={b.label}
                                    data={b}
                                    actionsSecondary={
                                        <Menu>
                                            <Menu.Item onClick={(e, row) => setDeleting(row)}>
                                                Delete
                                            </Menu.Item>
                                        </Menu>
                                    }
                                >
                                    <Table.Cell>
                                        <Mono>{b.label}</Mono>
                                    </Table.Cell>
                                    <Table.Cell>
                                        <Link
                                            onClick={() =>
                                                navigate(`run/${encodeURIComponent(b.run_id)}`)
                                            }
                                        >
                                            run {b.run_id}
                                        </Link>
                                    </Table.Cell>
                                    <Table.Cell>
                                        <Mono>{b.scenario}</Mono>
                                    </Table.Cell>
                                    <Table.Cell>
                                        <RelTime value={b.created_at} />
                                    </Table.Cell>
                                    <Table.Cell>{b.note || ''}</Table.Cell>
                                </Table.Row>
                            ))}
                        </Table.Body>
                    </Table>
                </Panel>
            )}

            {deleting && (
                <ConfirmModal
                    title="Delete baseline"
                    confirmLabel="Delete"
                    onClose={() => setDeleting(null)}
                    onConfirm={async () => {
                        await api(`/baselines/${encodeURIComponent(deleting.label)}`, {
                            method: 'DELETE',
                        });
                        notify('Baseline deleted', 'success');
                        reload();
                    }}
                >
                    <P>
                        Delete the baseline <strong>{deleting.label}</strong>? Run {deleting.run_id}{' '}
                        itself is untouched; any pipeline comparing against this label will stop
                        finding it.
                    </P>
                </ConfirmModal>
            )}
        </div>
    );
}

Baselines.propTypes = { navigate: PropTypes.func.isRequired };
