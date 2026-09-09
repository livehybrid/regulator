import React, { useRef, useState } from 'react';
import PropTypes from 'prop-types';
import Button from '@splunk/react-ui/Button';
import Card from '@splunk/react-ui/Card';
import ControlGroup from '@splunk/react-ui/ControlGroup';
import Heading from '@splunk/react-ui/Heading';
import Message from '@splunk/react-ui/Message';
import Text from '@splunk/react-ui/Text';
import styled from 'styled-components';
import { variables } from '@splunk/themes';

import { api, ApiError } from '../api';

const Centre = styled.div`
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    padding: ${variables.spacingLarge};
`;

const Sub = styled.div`
    color: ${variables.contentColorMuted};
    font-size: ${variables.fontSizeSmall};
    margin-bottom: ${variables.spacingMedium};
`;

const Form = styled.form`
    width: 320px;
`;

export default function Login({ error, onSignedIn }) {
    const [password, setPassword] = useState('');
    const [busy, setBusy] = useState(false);
    const [message, setMessage] = useState(error || null);
    const field = useRef(null);

    const submit = async (e) => {
        e.preventDefault();
        if (busy) {
            return;
        }
        setBusy(true);
        setMessage(null);
        try {
            await api('/auth/login', { method: 'POST', body: { password } });
            setPassword('');
            onSignedIn();
        } catch (err) {
            // A wrong password is the expected outcome here, not an incident:
            // say so plainly rather than showing a stack of API detail.
            setMessage(
                err instanceof ApiError && err.status === 401
                    ? 'That password was not accepted.'
                    : err.message
            );
            setBusy(false);
        }
    };

    return (
        <Centre>
            <Card>
                <Card.Body>
                    <Form onSubmit={submit}>
                        <Heading level={2}>Regulator</Heading>
                        <Sub>Search load generation for Splunk</Sub>
                        {message && (
                            <Message appearance="fill" type="error">
                                {message}
                            </Message>
                        )}
                        <ControlGroup label="Password" labelPosition="top">
                            <Text
                                type="password"
                                inputRef={field}
                                autoComplete="current-password"
                                autoFocus
                                value={password}
                                onChange={(e, { value }) => setPassword(value)}
                            />
                        </ControlGroup>
                        <Button
                            type="submit"
                            appearance="primary"
                            inline={false}
                            disabled={busy}
                            label={busy ? 'Signing in…' : 'Sign in'}
                        />
                    </Form>
                </Card.Body>
            </Card>
        </Centre>
    );
}

Login.propTypes = {
    error: PropTypes.string,
    onSignedIn: PropTypes.func.isRequired,
};
