// Shared HTMX wiring.
//
// The previous version of this file carried a 60-second client-side dashboard
// cache that could never run: it assigned xhr.onload and then cancelled the
// request via preventDefault(), so the request was never sent and the handler
// never fired. It also drove a four-step "progress" animation that started
// after the content had already arrived. Both are gone -- caching belongs on
// the server, and a progress bar that reports no progress is worse than none.

(function () {
    'use strict';

    // Attach the CSRF token to every mutating HTMX request.
    document.body.addEventListener('htmx:configRequest', function (event) {
        var method = (event.detail.verb || 'get').toLowerCase();
        if (method === 'get' || method === 'head') {
            return;
        }
        var meta = document.querySelector('meta[name="csrf-token"]');
        if (meta) {
            event.detail.headers['X-CSRFToken'] = meta.getAttribute('content');
        }
    });

    function syncActiveNav() {
        var params = new URLSearchParams(window.location.search);
        var currentView = params.get('view') || 'triage';
        document.querySelectorAll('.sidebar .nav-link').forEach(function (link) {
            var href = link.getAttribute('href') || '';
            var target = link.getAttribute('hx-get') || '';
            var view = link.getAttribute('data-view') || '';
            var isMatch = (href.indexOf('view=' + currentView) !== -1) || (view === currentView);
            link.classList.toggle('active', isMatch);
        });
    }

    // Restore collapsible MTS categories state across live polling swaps
    function restoreMtsCollapsibleState() {
        document.querySelectorAll('.mts-category-card').forEach(function (el) {
            var cat = el.getAttribute('data-cat');
            if (cat) {
                var saved = sessionStorage.getItem('mts_cat_' + cat + '_open');
                if (saved !== null) {
                    el.open = (saved === '1');
                }
            }
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        syncActiveNav();
        restoreMtsCollapsibleState();
    });

    window.addEventListener('popstate', function () {
        syncActiveNav();
        var params = new URLSearchParams(window.location.search);
        var currentView = params.get('view') || 'triage';
        var views = {
            'triage': '/fhir/Triage',
            'pipeline': '/pipeline/status',
            'patients': '/fhir/Patients',
            'stats': '/fhir/Demographics',
            'insights': '/fhir/Dashboard'
        };
        var targetUrl = views[currentView];
        var content = document.getElementById('content');
        if (content && targetUrl && window.htmx) {
            window.htmx.ajax('GET', targetUrl, { target: '#content', swap: 'innerHTML' });
        }
    });

    // Save toggle changes on MTS categories
    document.addEventListener('toggle', function (e) {
        if (e.target && e.target.classList && e.target.classList.contains('mts-category-card')) {
            var cat = e.target.getAttribute('data-cat');
            if (cat) {
                sessionStorage.setItem('mts_cat_' + cat + '_open', e.target.open ? '1' : '0');
            }
        }
    }, true);

    // Global toggle helpers accessible from window
    window.expandAllCategories = function (openState) {
        document.querySelectorAll('.mts-category-card').forEach(function (el) {
            el.open = Boolean(openState);
            var cat = el.getAttribute('data-cat');
            if (cat) {
                sessionStorage.setItem('mts_cat_' + cat + '_open', openState ? '1' : '0');
            }
        });
    };

    window.collapseNonUrgent = function () {
        document.querySelectorAll('.mts-category-card').forEach(function (el) {
            var cat = parseInt(el.getAttribute('data-cat') || '0', 10);
            var shouldOpen = (cat <= 3);
            el.open = shouldOpen;
            sessionStorage.setItem('mts_cat_' + cat + '_open', shouldOpen ? '1' : '0');
        });
    };

    // Highlight the sidebar entry matching whatever was just loaded.
    document.body.addEventListener('htmx:afterSwap', function (event) {
        syncActiveNav();
        restoreMtsCollapsibleState();
    });

    // Surface failures instead of leaving a panel silently blank.
    document.body.addEventListener('htmx:responseError', function (event) {
        var target = event.detail.target;
        if (target && !target.innerHTML.trim()) {
            target.innerHTML =
                '<div class="alert alert-danger mb-0">' +
                '<i class="fas fa-exclamation-triangle me-2"></i>' +
                'This section could not be loaded. Please try again.</div>';
        }
    });

    document.body.addEventListener('htmx:sendError', function (event) {
        var target = event.detail.target;
        if (target) {
            target.innerHTML =
                '<div class="alert alert-danger mb-0">' +
                '<i class="fas fa-plug me-2"></i>' +
                'Lost connection to the server. Check your network and try again.</div>';
        }
    });
})();
