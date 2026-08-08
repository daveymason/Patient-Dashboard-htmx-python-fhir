document.addEventListener('DOMContentLoaded', function () {
    'use strict';

    var apiKeyInput = document.getElementById('openaiApiKey');
    var testButton = document.getElementById('testApiKeyBtn');
    var resultDiv = document.getElementById('apiKeyTestResult');
    if (!testButton || !apiKeyInput || !resultDiv) {
        return;
    }

    function setResult(message, tone) {
        resultDiv.textContent = message;
        resultDiv.className = 'mt-3 ' + (tone ? 'text-' + tone : '');
    }

    function csrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    testButton.addEventListener('click', async function () {
        var apiKey = apiKeyInput.value.trim();
        if (!apiKey) {
            setResult('Enter an API key first.', 'danger');
            return;
        }

        testButton.disabled = true;
        setResult('Checking...', 'muted');

        try {
            var response = await fetch('/test_openai_key', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken()
                },
                body: JSON.stringify({ api_key: apiKey })
            });
            var data = await response.json();
            setResult(data.message || 'Unexpected response.', data.success ? 'success' : 'danger');
        } catch (error) {
            console.error('Key test failed:', error);
            setResult('The key could not be checked. Check your connection and try again.', 'danger');
        } finally {
            testButton.disabled = false;
        }
    });
});
