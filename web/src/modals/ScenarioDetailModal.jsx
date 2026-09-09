import React from 'react';
import PropTypes from 'prop-types';
import Chip from '@splunk/react-ui/Chip';
import Code from '@splunk/react-ui/Code';
import DL from '@splunk/react-ui/DefinitionList';
import List from '@splunk/react-ui/List';
import Message from '@splunk/react-ui/Message';
import P from '@splunk/react-ui/Paragraph';
import Table from '@splunk/react-ui/Table';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';

import BusyButton from '../components/BusyButton';
import Dialog from '../components/Dialog';
import { Mono, SectionHeading } from '../components/primitives';
import { fmtNum } from '../format';

export default function ScenarioDetailModal({ scenario, loading, onClose, onDelete }) {
    const d = scenario || {};
    const skipped = Object.entries(d.saved_skipped || {});
    const files = Object.entries(d.files || {});

    return (
        <Dialog
            title={d.name || 'Scenario'}
            width="1080px"
            onClose={onClose}
            footer={
                d.origin === 'user' && (
                    <BusyButton
                        appearance="destructive"
                        label="Delete scenario"
                        onClick={async () => {
                            await onDelete();
                            onClose();
                        }}
                    />
                )
            }
        >
            {loading ? (
                <P>
                    <WaitSpinner /> Loading the scenario&hellip;
                </P>
            ) : (
                <>
                    <P>{d.description || ''}</P>
                    <DL termWidth="150px">
                        <DL.Term>Origin</DL.Term>
                        <DL.Description>{d.origin}</DL.Description>
                        <DL.Term>Load model</DL.Term>
                        <DL.Description>{d.load_model}</DL.Description>
                        <DL.Term>Digest</DL.Term>
                        <DL.Description>
                            <Mono>{d.digest || ''}</Mono>
                        </DL.Description>
                        <DL.Term>Sourcetypes</DL.Term>
                        <DL.Description>
                            {(d.sourcetypes || []).join(', ') || 'none declared'}
                        </DL.Description>
                    </DL>

                    {d.runnable_here === false && (
                        <Message appearance="fill" type="warning">
                            <Message.Title>Not runnable from this control plane</Message.Title>
                            {d.not_runnable_reason}
                        </Message>
                    )}

                    <SectionHeading>Steps</SectionHeading>
                    <Table horizontalOverflow="scroll">
                        <Table.Head>
                            <Table.HeadCell>Persona</Table.HeadCell>
                            <Table.HeadCell>Step</Table.HeadCell>
                            <Table.HeadCell>Class</Table.HeadCell>
                            <Table.HeadCell>Cron</Table.HeadCell>
                            <Table.HeadCell align="right">Weight</Table.HeadCell>
                            <Table.HeadCell>Search</Table.HeadCell>
                        </Table.Head>
                        <Table.Body>
                            {(d.steps || []).map((st) => (
                                <Table.Row key={`${st.persona}:${st.id}`}>
                                    <Table.Cell>
                                        <Mono>{st.persona}</Mono>
                                    </Table.Cell>
                                    <Table.Cell>
                                        <Mono>{st.id}</Mono>
                                    </Table.Cell>
                                    <Table.Cell>
                                        <Chip appearance="outline">{st.class}</Chip>
                                    </Table.Cell>
                                    <Table.Cell>
                                        <Mono>{st.cron || ''}</Mono>
                                    </Table.Cell>
                                    <Table.Cell align="right">
                                        {st.weight ? fmtNum(st.weight, 2) : ''}
                                    </Table.Cell>
                                    <Table.Cell>
                                        <Mono $break>
                                            {st.spl ||
                                                (st.dashboard ? `${st.app}/${st.dashboard}` : '')}
                                        </Mono>
                                        {st.saved && (
                                            <div>
                                                <Mono>
                                                    from saved search {st.saved}
                                                    {st.dispatch === 'saved'
                                                        ? ', dispatched by name on the target'
                                                        : ''}
                                                </Mono>
                                            </div>
                                        )}
                                    </Table.Cell>
                                </Table.Row>
                            ))}
                        </Table.Body>
                    </Table>

                    {skipped.length > 0 && (
                        <>
                            <SectionHeading>Left out of the saved searches, and why</SectionHeading>
                            <List>
                                {skipped.map(([k, v]) => (
                                    <List.Item key={k}>
                                        <Mono>{k}</Mono>: {v}
                                    </List.Item>
                                ))}
                            </List>
                        </>
                    )}

                    {files.map(([name, content]) => (
                        <div key={name}>
                            <SectionHeading>{name}</SectionHeading>
                            <Code
                                value={content}
                                language={name.endsWith('.yaml') || name.endsWith('.yml') ? 'yaml' : 'plaintext'}
                                containerAppearance="section"
                                lineWrap
                            />
                        </div>
                    ))}
                </>
            )}
        </Dialog>
    );
}

ScenarioDetailModal.propTypes = {
    scenario: PropTypes.object,
    loading: PropTypes.bool,
    onClose: PropTypes.func.isRequired,
    onDelete: PropTypes.func,
};
