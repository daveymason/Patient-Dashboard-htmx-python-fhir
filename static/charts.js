// Chart.js lifecycle for HTMX-swapped panels.
//
// Chart config is embedded per-canvas as a <script type="application/json"
// data-chart-for="canvas-id"> sibling rather than as an inline <script> that
// builds the chart directly -- JSON.parse of textContent needs no HTML-attribute
// escaping and can't execute anything, unlike a hand-built inline script tag.
//
// Chart.js instances aren't garbage-collected just because their canvas leaves
// the DOM, so every chart is destroyed via htmx:beforeCleanupElement (fired once
// per removed node, including descendants) and (re)built via htmx:load (fired
// for the initial page load and every swap) -- the pairing htmx documents for
// third-party widget teardown.

(function () {
    'use strict';

    function initCharts(root) {
        if (!root || typeof root.querySelectorAll !== 'function') {
            return;
        }
        root.querySelectorAll('script[data-chart-for]').forEach(function (configEl) {
            var canvas = document.getElementById(configEl.getAttribute('data-chart-for'));
            if (!canvas || canvas._chartInstance) {
                return;
            }
            var config;
            try {
                config = JSON.parse(configEl.textContent);
            } catch (error) {
                console.error('Invalid chart configuration for', canvas.id, error);
                return;
            }
            canvas._chartInstance = new Chart(canvas.getContext('2d'), config);
        });
    }

    document.body.addEventListener('htmx:load', function (event) {
        initCharts(event.target);
    });

    document.body.addEventListener('htmx:beforeCleanupElement', function (event) {
        var el = event.target;
        if (el && el.tagName === 'CANVAS' && el._chartInstance) {
            el._chartInstance.destroy();
            el._chartInstance = null;
        }
    });
})();
