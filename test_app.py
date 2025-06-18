import os
import unittest
from unittest.mock import patch, MagicMock
import json
import requests # Added for requests.exceptions.HTTPError

# Import Flask app and openai from the main application module
# Assuming your Flask app instance is named 'app' in 'app.py'
from app import app, openai # Make sure 'openai' is importable if you mock its classes directly

# Configure app for testing
app.config['TESTING'] = True
app.config['SECRET_KEY'] = 'test_secret_key' # Example, can be anything for testing

class TestSummarizePatientRoute(unittest.TestCase):

    def setUp(self):
        self.client = app.test_client()
        # It's good practice to save and restore environment variables if tests modify them
        self.original_openai_api_key = os.environ.get('OPENAI_API_KEY')

        # Mock login_required decorator or current_user
        # Patching _get_user which is used by current_user
        self.patcher = patch('flask_login.utils._get_user')
        self.mock_get_user = self.patcher.start()

        # Configure the mock to return a mock user object that is authenticated
        mock_user = MagicMock()
        mock_user.is_authenticated = True
        self.mock_get_user.return_value = mock_user


    def tearDown(self):
        # Restore environment variables
        if self.original_openai_api_key is None:
            if 'OPENAI_API_KEY' in os.environ:
                del os.environ['OPENAI_API_KEY']
        else:
            os.environ['OPENAI_API_KEY'] = self.original_openai_api_key

        # Stop the patcher
        self.patcher.stop()

    # Test Case 1: Successful Summarization
    @patch('app.requests.get') # Patch requests.get used in app.py
    @patch('openai.OpenAI') # Patch the OpenAI client constructor
    def test_successful_summarization(self, mock_openai_constructor, mock_requests_get):
        os.environ['OPENAI_API_KEY'] = 'test_api_key'

        # --- Mocking FHIR Patient Data Call ---
        mock_fhir_patient_response = MagicMock()
        mock_fhir_patient_response.status_code = 200
        mock_fhir_patient_response.json.return_value = {
            "id": "test_patient_123",
            "name": [{"given": ["John"], "family": "Doe"}],
            "gender": "male",
            "birthDate": "1970-01-01"
        }

        # --- Mocking FHIR Condition Data Call ---
        mock_fhir_condition_response = MagicMock()
        mock_fhir_condition_response.status_code = 200
        mock_fhir_condition_response.json.return_value = {
            "entry": [
                {"resource": {"code": {"text": "Hypertension"}}},
                {"resource": {"code": {"text": "Diabetes"}}}
            ]
        }

        # Configure mock_requests_get to return different values based on URL
        def side_effect_requests_get(url, timeout=None):
            if "Patient/test_patient_123" in url:
                return mock_fhir_patient_response
            elif "Condition?patient=test_patient_123" in url:
                return mock_fhir_condition_response
            return MagicMock(status_code=404) # Default for unexpected calls

        mock_requests_get.side_effect = side_effect_requests_get

        # --- Mocking OpenAI API Call ---
        mock_openai_instance = MagicMock()
        mock_chat_completion = MagicMock()
        mock_chat_completion.choices = [MagicMock(message=MagicMock(content="This is a test summary."))]
        mock_openai_instance.chat.completions.create.return_value = mock_chat_completion
        mock_openai_constructor.return_value = mock_openai_instance # Constructor returns our mocked instance

        # Call the route
        response = self.client.get('/summarize_patient/test_patient_123')
        response_data = json.loads(response.data.decode('utf-8'))

        # Assertions
        self.assertEqual(response.status_code, 200)
        self.assertIn("summary", response_data)
        self.assertEqual(response_data["summary"], "This is a test summary.")
        self.assertEqual(response_data["patient_id"], "test_patient_123")

        # Verify OpenAI client was called with correct model
        mock_openai_instance.chat.completions.create.assert_called_once()
        call_args = mock_openai_instance.chat.completions.create.call_args
        self.assertEqual(call_args.kwargs['model'], "gpt-4.1-nano-2025-04-14")


    # Test Case 2: Missing OpenAI API Key
    def test_missing_openai_api_key(self):
        if 'OPENAI_API_KEY' in os.environ:
            del os.environ['OPENAI_API_KEY'] # Ensure key is not set

        response = self.client.get('/summarize_patient/test_patient_123')
        response_data = json.loads(response.data.decode('utf-8'))

        self.assertEqual(response.status_code, 500)
        self.assertIn("error", response_data)
        self.assertEqual(response_data["error"], "OpenAI API key is not configured on the server.")

    # Test Case 3: OpenAI API Error (AuthenticationError)
    @patch('app.requests.get')
    @patch('openai.OpenAI')
    def test_openai_authentication_error(self, mock_openai_constructor, mock_requests_get):
        os.environ['OPENAI_API_KEY'] = 'invalid_key'

        # Mock FHIR calls to be successful
        mock_fhir_patient_response = MagicMock()
        mock_fhir_patient_response.status_code = 200
        mock_fhir_patient_response.json.return_value = {"id": "p1", "name": [{"given": ["Test"], "family": "Patient"}]}

        mock_fhir_condition_response = MagicMock()
        mock_fhir_condition_response.status_code = 200
        mock_fhir_condition_response.json.return_value = {"entry": []}

        def side_effect_requests_get(url, timeout=None):
            if "Patient/test_patient_123" in url:
                return mock_fhir_patient_response
            elif "Condition?patient=test_patient_123" in url:
                return mock_fhir_condition_response
            return MagicMock(status_code=404)
        mock_requests_get.side_effect = side_effect_requests_get

        # Mock OpenAI client to raise AuthenticationError
        mock_openai_instance = MagicMock()
        # Create a mock response object that openai.AuthenticationError expects
        mock_http_response = MagicMock()
        mock_http_response.request = None # Or a MagicMock for a request object if further attributes are accessed
        mock_openai_instance.chat.completions.create.side_effect = openai.AuthenticationError(message="Invalid API key", response=mock_http_response, body=None)
        mock_openai_constructor.return_value = mock_openai_instance

        response = self.client.get('/summarize_patient/test_patient_123')
        response_data = json.loads(response.data.decode('utf-8'))

        self.assertEqual(response.status_code, 401)
        self.assertIn("error", response_data)
        self.assertEqual(response_data["error"], "AI service authentication failed. Please check the configured API key.")

    # Test Case 3b: OpenAI API Error (General APIError)
    @patch('app.requests.get')
    @patch('openai.OpenAI')
    def test_openai_general_api_error(self, mock_openai_constructor, mock_requests_get):
        os.environ['OPENAI_API_KEY'] = 'valid_key_but_service_down'

        mock_fhir_patient_response = MagicMock()
        mock_fhir_patient_response.status_code = 200
        mock_fhir_patient_response.json.return_value = {"id": "p1", "name": [{"given": ["Test"], "family": "Patient"}]}

        mock_fhir_condition_response = MagicMock()
        mock_fhir_condition_response.status_code = 200
        mock_fhir_condition_response.json.return_value = {"entry": []}

        def side_effect_requests_get(url, timeout=None):
            if "Patient/test_patient_123" in url:
                return mock_fhir_patient_response
            elif "Condition?patient=test_patient_123" in url:
                return mock_fhir_condition_response
            return MagicMock(status_code=404)
        mock_requests_get.side_effect = side_effect_requests_get

        mock_openai_instance = MagicMock()
        # For a generic APIError, 'response' might not be expected in constructor.
        # Let's provide a mock request object as it's often expected by APIError.
        mock_request = MagicMock()
        mock_openai_instance.chat.completions.create.side_effect = openai.APIError(message="Service unavailable", request=mock_request, body=None)
        mock_openai_constructor.return_value = mock_openai_instance

        response = self.client.get('/summarize_patient/test_patient_123')
        response_data = json.loads(response.data.decode('utf-8'))

        self.assertEqual(response.status_code, 500)
        self.assertIn("error", response_data)
        self.assertEqual(response_data["error"], "Failed to get summary from AI service due to an API error. Please try again later.")

    # Test Case 4: FHIR API Error (Patient Data)
    @patch('app.requests.get') # Patch requests.get used in app.py
    def test_fhir_api_error_patient_data(self, mock_requests_get):
        os.environ['OPENAI_API_KEY'] = 'test_api_key'

        # Mock FHIR Patient Data Call to return an error
        mock_fhir_patient_response = MagicMock()
        mock_fhir_patient_response.status_code = 404
        mock_fhir_patient_response.reason = "Not Found"
        # raise_for_status is called in the route, so we need to mock that behavior for HTTPError
        mock_fhir_patient_response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_fhir_patient_response)

        # Configure mock_requests_get
        # Only the patient call will be made before it errors out
        mock_requests_get.return_value = mock_fhir_patient_response

        response = self.client.get('/summarize_patient/test_patient_fhir_error')
        response_data = json.loads(response.data.decode('utf-8'))

        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response_data)
        self.assertEqual(response_data["error"], "Failed to fetch patient data from FHIR server: 404 Not Found")

    # Test Case 5: FHIR API Error (Condition Data) - Bonus
    @patch('app.requests.get') # Patch requests.get used in app.py
    @patch('openai.OpenAI') # Patch the OpenAI client constructor
    def test_fhir_api_error_condition_data(self, mock_openai_constructor, mock_requests_get):
        os.environ['OPENAI_API_KEY'] = 'test_api_key'

        mock_fhir_patient_response = MagicMock()
        mock_fhir_patient_response.status_code = 200
        mock_fhir_patient_response.json.return_value = {
            "id": "test_patient_123",
            "name": [{"given": ["John"], "family": "Doe"}],
            "gender": "male",
            "birthDate": "1970-01-01"
        }

        mock_fhir_condition_response = MagicMock()
        mock_fhir_condition_response.status_code = 500
        mock_fhir_condition_response.reason = "Server Error"
        mock_fhir_condition_response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_fhir_condition_response)


        # Configure mock_requests_get to return different values based on URL
        # The route currently doesn't raise_for_status on the condition call, nor does it check status explicitly for it.
        # It just proceeds if conditions_response.status_code == 200. So, if not 200, it just skips adding conditions.
        # This test will therefore still result in a 200 from the main route, but patient_info_for_prompt won't have conditions.
        # This behavior is based on the current implementation of summarize_patient in app.py.
        # If the requirements were to error out on condition fetch failure, app.py would need to change.

        # Let's adjust the test to reflect the *current* behavior: a non-200 on conditions means they are just omitted.
        mock_fhir_condition_response_non_200 = MagicMock()
        mock_fhir_condition_response_non_200.status_code = 500
        mock_fhir_condition_response_non_200.json.return_value = {"error": "some error"}


        def side_effect_requests_get(url, timeout=None):
            if "Patient/test_patient_123" in url:
                return mock_fhir_patient_response
            elif "Condition?patient=test_patient_123" in url:
                # This is the one we are testing for non-200 status
                return mock_fhir_condition_response_non_200
            return MagicMock(status_code=404)

        mock_requests_get.side_effect = side_effect_requests_get

        mock_openai_instance = MagicMock()
        mock_chat_completion = MagicMock()
        mock_chat_completion.choices = [MagicMock(message=MagicMock(content="Summary without conditions."))]
        mock_openai_instance.chat.completions.create.return_value = mock_chat_completion
        mock_openai_constructor.return_value = mock_openai_instance

        response = self.client.get('/summarize_patient/test_patient_123')
        response_data = json.loads(response.data.decode('utf-8'))

        self.assertEqual(response.status_code, 200) # Still 200 because condition error is not fatal in current app.py
        self.assertIn("summary", response_data)
        self.assertEqual(response_data["summary"], "Summary without conditions.")

        # Check that the prompt sent to OpenAI does not contain "Key Conditions"
        # This requires inspecting the arguments to the mocked OpenAI call
        _, openai_call_kwargs = mock_openai_instance.chat.completions.create.call_args
        prompt_messages = openai_call_kwargs['messages']
        user_prompt_content = ""
        for msg in prompt_messages:
            if msg['role'] == 'user':
                user_prompt_content = msg['content']
                break
        self.assertNotIn("Key Conditions:", user_prompt_content)


if __name__ == '__main__':
    unittest.main()
