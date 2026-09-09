/*
 * Builds the operator console into ../server/ui, which is what the control
 * plane serves.
 *
 * Everything is bundled and self-hosted: no CDN, no external font, no runtime
 * fetch of a library. Regulator is pointed at production Splunk clusters and
 * is frequently run in an environment with no route to the internet, so a page
 * that needs to reach unpkg to render is a page that does not render at all.
 */
const path = require('path');
const HtmlWebpackPlugin = require('html-webpack-plugin');
const TerserPlugin = require('terser-webpack-plugin');
const { merge: webpackMerge } = require('webpack-merge');
const baseConfig = require('@splunk/webpack-configs/base.config').default;

const OUT = path.resolve(__dirname, '..', 'server', 'ui');

module.exports = webpackMerge(baseConfig, {
    entry: path.join(__dirname, 'src', 'index.jsx'),
    output: {
        path: OUT,
        // Content-hashed, because the server sends index.html with
        // Cache-Control: no-store but the bundle is immutable per build: a new
        // build gets a new name rather than a stale cached script.
        filename: 'assets/regulator.[contenthash:8].js',
        chunkFilename: 'assets/regulator.[name].[contenthash:8].js',
        publicPath: '/',
        // The whole directory is generated. Nothing here is hand-edited, so a
        // rename never leaves an orphan bundle behind to be served forever.
        clean: true,
    },
    plugins: [
        new HtmlWebpackPlugin({
            template: path.join(__dirname, 'src', 'index.html'),
            filename: 'index.html',
            inject: 'body',
            scriptLoading: 'defer',
        }),
    ],
    // The Splunk chart library is a very large dependency graph, and a cold
    // production build of it takes minutes. The filesystem cache makes every
    // build after the first one fast, which is the difference between `make
    // ui-watch` being usable and not. It lives in node_modules, so it is
    // already ignored and is thrown away with a reinstall.
    cache: {
        type: 'filesystem',
        cacheDirectory: path.resolve(__dirname, 'node_modules', '.cache', 'webpack'),
        buildDependencies: { config: [__filename] },
    },
    optimization: {
        minimizer: [
            // Terser defaults to one worker per core, each with its own heap.
            // The chart library is a single very large chunk, and seven copies
            // of it in flight is enough to be OOM-killed inside a 4 GB
            // container: the build dies with an exit code and no message,
            // which reads as a hang rather than a memory limit. Two workers
            // build it comfortably and cost about a minute.
            new TerserPlugin({ parallel: 2 }),
        ],
    },
    performance: {
        // The Splunk component library is large and is deliberately shipped
        // whole; a size warning here is noise, not a finding.
        hints: false,
    },
});
