// Splunk's own lint rules, the same set CIMplicity uses, so the two code bases
// read the same way. The overrides are the two places this project genuinely
// differs, and both say why.
module.exports = {
    // browser-prettier is what CIMplicity extends. It pulls in
    // eslint-config-prettier, but at v6 that package keeps its React
    // formatting rules in a separate entry point, so 'prettier/react' has to
    // be listed too or JSX line-breaking is linted twice, once by prettier's
    // rules and once against them.
    extends: ['@splunk/eslint-config/browser-prettier', 'prettier/react'],
    parser: '@babel/eslint-parser',
    parserOptions: {
        requireConfigFile: false,
        babelOptions: { presets: ['@splunk/babel-preset'] },
    },
    rules: {
        // The nested ternary is how a theme token is picked from a status in
        // this code base, and hoisting each one into a lookup object hurts
        // more than it helps at three branches.
        'no-nested-ternary': 'off',
        // Dialogs and pages take a settled API object and hand parts of it
        // straight to a child; shape-checking each one would be a second copy
        // of the server's schema, kept in sync by hand.
        'react/forbid-prop-types': 'off',
    },
    overrides: [
        {
            // The smoke test is a Node script that stands up a fake browser.
            // It is linted with the Node rules, and the exceptions below are
            // what standing up a fake browser actually requires.
            files: ['smoke.mjs'],
            extends: ['@splunk/eslint-config/node-prettier'],
            env: { node: true, browser: false },
            rules: {
                // It is a test harness: printing the result is the point.
                'no-console': 'off',
                // jsdom is a test dependency and belongs in devDependencies.
                'import/no-extraneous-dependencies': ['error', { devDependencies: true }],
                // The ResizeObserver stub is a stub: its methods do nothing on
                // purpose, so none of them touch `this`.
                'class-methods-use-this': 'off',
                // Two: the ResizeObserver stub and the loader that serves the
                // build output. Both are browser pieces jsdom does not provide
                // and neither belongs in its own file.
                'max-classes-per-file': ['error', 2],
            },
        },
    ],
};
