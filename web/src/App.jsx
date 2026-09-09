/*
 * The application shell: theme, session, navigation.
 *
 * The whole console is rendered from Splunk's own component library so that an
 * operator moving between Splunk Web and Regulator is not switching design
 * languages half way through a capacity test. Where a Splunk component exists
 * for the job, it is the one used; the exceptions are the run charts (see
 * components/LineChart.jsx) and the layout scaffolding, and both are built
 * from @splunk/themes tokens rather than invented colours.
 */
import React, { useCallback, useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { SplunkThemeProvider, variables } from '@splunk/themes';
import Button from '@splunk/react-ui/Button';
import Divider from '@splunk/react-ui/Divider';
import Menu from '@splunk/react-ui/Menu';
import MessageBar from '@splunk/react-ui/MessageBar';
import Moon from '@splunk/react-icons/Moon';
import Plus from '@splunk/react-icons/Plus';
import Sun from '@splunk/react-icons/Sun';
import WaitSpinner from '@splunk/react-ui/WaitSpinner';
import styled, { createGlobalStyle } from 'styled-components';

import Audit from './pages/Audit';
import Baselines from './pages/Baselines';
import LaunchModal from './modals/LaunchModal';
import Login from './pages/Login';
import RunDetail from './pages/RunDetail';
import Runs from './pages/Runs';
import Scenarios from './pages/Scenarios';
import TargetDetail from './pages/TargetDetail';
import Targets from './pages/Targets';
import { NotificationProvider } from './Notifications';
import { api, isAuthError, setUnauthorisedHandler } from './api';
import { parseRoute, useHashRoute } from './useHashRoute';

const THEME_KEY = 'regulator.colorScheme';

const GlobalStyle = createGlobalStyle`
    html, body, #regulator-root { height: 100%; }
    body {
        margin: 0;
        background-color: ${variables.backgroundColorPage};
        color: ${variables.contentColorDefault};
        font-family: ${variables.fontFamily};
        font-size: ${variables.fontSize};
    }
    * { box-sizing: border-box; }
`;

const Shell = styled.div`
    display: grid;
    grid-template-columns: 208px minmax(0, 1fr);
    min-height: 100vh;
`;

const Sidebar = styled.nav`
    background-color: ${variables.backgroundColorSidebar};
    border-right: 1px solid ${variables.borderColor};
    padding: ${variables.spacingMedium} ${variables.spacingSmall};
    display: flex;
    flex-direction: column;
    gap: ${variables.spacingSmall};
`;

const Brand = styled.div`
    padding: ${variables.spacingSmall} ${variables.spacingSmall} ${variables.spacingMedium};
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: ${variables.contentColorAccent};
`;

const BrandSub = styled.div`
    display: block;
    margin-top: 3px;
    font-size: ${variables.fontSizeSmall};
    letter-spacing: 0.02em;
    text-transform: none;
    font-weight: 400;
    color: ${variables.contentColorMuted};
`;

const Spacer = styled.div`
    flex: 1;
`;

const Main = styled.main`
    padding: ${variables.spacingMedium} ${variables.spacingLarge} 64px;
    min-width: 0;
`;

const Centre = styled.div`
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    gap: ${variables.spacingSmall};
    color: ${variables.contentColorMuted};
`;

const NAV = [
    { route: 'targets', label: 'Targets' },
    { route: 'scenarios', label: 'Scenarios' },
    { route: 'runs', label: 'Runs' },
    { route: 'baselines', label: 'Baselines' },
    { route: 'audit', label: 'Audit' },
];

// A run page belongs under Runs, and a target's history under Targets, so the
// navigation does not go blank the moment you open a detail view.
const NAV_PARENT = { run: 'runs', target: 'targets' };

function readScheme() {
    try {
        const stored = window.localStorage.getItem(THEME_KEY);
        if (stored === 'dark' || stored === 'light') {
            return stored;
        }
    } catch (e) {
        // A browser with storage disabled is not a reason to fail to render.
    }
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches
        ? 'light'
        : 'dark';
}

export default function App() {
    const [colorScheme, setColorScheme] = useState(readScheme);
    // null while the first /auth/status call is in flight, so the sign-in form
    // does not flash in front of an operator who already has a session.
    const [session, setSession] = useState(null);

    useEffect(() => {
        try {
            window.localStorage.setItem(THEME_KEY, colorScheme);
        } catch (e) {
            // Not worth a message: the theme simply will not be remembered.
        }
    }, [colorScheme]);

    const loadStatus = useCallback(async () => {
        try {
            const s = await api('/auth/status');
            setSession({ authenticated: !!s.authenticated, setupNeeded: !!s.setup_needed });
        } catch (e) {
            if (isAuthError(e)) {
                setSession({ authenticated: false, setupNeeded: false });
            } else {
                setSession({ authenticated: false, setupNeeded: false, error: e.message });
            }
        }
    }, []);

    useEffect(() => {
        setUnauthorisedHandler(() =>
            setSession((prev) => (prev && prev.authenticated ? { ...prev, authenticated: false } : prev))
        );
        loadStatus();
    }, [loadStatus]);

    return (
        <SplunkThemeProvider family="enterprise" colorScheme={colorScheme} density="compact">
            <GlobalStyle />
            <NotificationProvider>
                {session === null ? (
                    <Centre>
                        <WaitSpinner size="medium" /> Checking the session&hellip;
                    </Centre>
                ) : session.authenticated ? (
                    <SignedIn
                        session={session}
                        colorScheme={colorScheme}
                        onToggleTheme={() => setColorScheme((s) => (s === 'dark' ? 'light' : 'dark'))}
                        onSignOut={loadStatus}
                    />
                ) : (
                    <Login error={session.error} onSignedIn={loadStatus} />
                )}
            </NotificationProvider>
        </SplunkThemeProvider>
    );
}

function SignedIn({ session, colorScheme, onToggleTheme, onSignOut }) {
    const [route, navigate] = useHashRoute();
    const { page, id } = parseRoute(route);
    const [launchFor, setLaunchFor] = useState(null);
    const active = NAV_PARENT[page] || page;

    const handleSignOut = async () => {
        try {
            await api('/auth/logout', { method: 'POST' });
        } catch (e) {
            // Signing out locally matters more than the server agreeing.
        }
        onSignOut();
    };

    return (
        <Shell>
            <Sidebar>
                <Brand>
                    Regulator
                    <BrandSub>Search load for Splunk</BrandSub>
                </Brand>
                <Menu appearance="subtle">
                    {NAV.map((item) => (
                        <Menu.Item
                            key={item.route}
                            to={`#${item.route}`}
                            selectable
                            selected={active === item.route}
                        >
                            {item.label}
                        </Menu.Item>
                    ))}
                </Menu>
                <Button
                    appearance="primary"
                    icon={<Plus />}
                    label="Launch a run"
                    onClick={() => setLaunchFor({ scenario: null })}
                />
                <Spacer />
                <Divider />
                <Button
                    appearance="subtle"
                    icon={colorScheme === 'dark' ? <Sun /> : <Moon />}
                    label={colorScheme === 'dark' ? 'Light theme' : 'Dark theme'}
                    onClick={onToggleTheme}
                />
                {!session.setupNeeded && (
                    <Button appearance="subtle" label="Sign out" onClick={handleSignOut} />
                )}
            </Sidebar>
            <Main>
                {session.setupNeeded && (
                    <MessageBar type="error" aria-label="Authentication warning">
                        <strong>This control plane has no password.</strong> Anyone who can reach it
                        can add targets, read their reports and drive load at your cluster. Set an
                        admin password in the server configuration and restart.
                    </MessageBar>
                )}
                <Page page={page} id={id} navigate={navigate} onLaunch={setLaunchFor} />
            </Main>
            {launchFor && (
                <LaunchModal
                    scenario={launchFor.scenario}
                    onClose={() => setLaunchFor(null)}
                    onLaunched={(runId) => {
                        setLaunchFor(null);
                        navigate(`run/${encodeURIComponent(runId)}`);
                    }}
                />
            )}
        </Shell>
    );
}

SignedIn.propTypes = {
    session: PropTypes.shape({ setupNeeded: PropTypes.bool }).isRequired,
    colorScheme: PropTypes.oneOf(['dark', 'light']).isRequired,
    onToggleTheme: PropTypes.func.isRequired,
    onSignOut: PropTypes.func.isRequired,
};

function Page({ page, id, navigate, onLaunch }) {
    switch (page) {
        case 'run':
            return <RunDetail runId={id} navigate={navigate} />;
        case 'target':
            return <TargetDetail targetId={id} navigate={navigate} />;
        case 'scenarios':
            return <Scenarios onLaunch={onLaunch} />;
        case 'runs':
            return <Runs navigate={navigate} onLaunch={onLaunch} />;
        case 'baselines':
            return <Baselines navigate={navigate} />;
        case 'audit':
            return <Audit />;
        default:
            return <Targets navigate={navigate} onLaunch={onLaunch} />;
    }
}

Page.propTypes = {
    page: PropTypes.string.isRequired,
    id: PropTypes.string,
    navigate: PropTypes.func.isRequired,
    onLaunch: PropTypes.func.isRequired,
};
