from flask import Flask, render_template, jsonify, request, session, redirect, url_for
import requests
import logging
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from datetime import datetime
import os  # Add os module for environment variables
import openai
import binascii
from eth_account.messages import defunct_hash_message, encode_defunct
from eth_account import Account

app = Flask(__name__)
app.secret_key = os.urandom(24) # Needed for Flask session management

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login' # Or your preferred login route

# Use environment variable with fallback to current value
FHIR_SERVER = os.environ.get('FHIR_SERVER_URL', 'http://hapi.fhir.org/baseR4')

# Set up logging
logging.basicConfig(level=logging.INFO)

# User model for Flask-Login
class User(UserMixin):
    def __init__(self, id):
        self.id = id # wallet_address will be the id

    @staticmethod
    def get(user_id):
        # This is a simplified 'get' method. In a real app, you might query a database.
        # For this example, if a user_id is provided, we create a User object.
        # Flask-Login uses this to manage the user session.
        return User(user_id)

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

@app.route('/')
def index():
    # Redirect unauthenticated users to the dedicated login page
    if not current_user.is_authenticated:
        return redirect(url_for('login'))
    return render_template('index.html')

@app.route('/health')
def health_check():
    """Simple health check endpoint for monitoring"""
    return jsonify({"status": "ok", "fhir_server": FHIR_SERVER})

def process_patient_results(patients):
    """Process patient results for consistent display"""
    processed_patients = []
    
    for patient_entry in patients:
        resource = patient_entry['resource']

        # Extract name
        name = resource.get('name', [{'given': ['Unknown'], 'family': ''}])[0]
        full_name = ' '.join(name.get('given', ['Unknown']) + [name.get('family', '')])

        # Extract gender
        gender = resource.get('gender', 'Unknown')

        # Extract birth date
        birth_date = resource.get('birthDate', 'Unknown')

        # Extract address
        address_info = resource.get('address', [{'line': ['Unknown Address']}])[0]
        address_parts = address_info.get('line', [])
        address_parts.extend([
            address_info.get('city', ''),
            address_info.get('state', ''),
            address_info.get('postalCode', '')
        ])
        address = ', '.join(filter(None, address_parts))  # Filter out empty strings

        # Extract telecom
        telecom_info = resource.get('telecom', [{'value': 'Unknown Contact'}])[0]
        telecom = telecom_info.get('value', 'Unknown Contact')

        processed_patient = {
            "id": resource.get('id', 'Unknown'),
            "name": full_name,
            "gender": gender,
            "birthDate": birth_date,
            "address": address,
            "telecom": telecom,
        }

        processed_patients.append(processed_patient)
        
    return processed_patients

def get_patients_table_body():
    """Helper to get patient table body for reuse"""
    response = requests.get(f"{FHIR_SERVER}/Patient?_count=10", timeout=10)  # Added timeout
    if response.status_code == 200:
        patients = response.json().get('entry', [])
        processed_patients = process_patient_results(patients)
        return render_template('patient_table_body.html', patients=processed_patients)
    else:
        return "<tr><td colspan='7'>Error loading patients</td></tr>", 500

@app.route('/fhir/Patients')
@login_required
def get_patients():
    response = requests.get(f"{FHIR_SERVER}/Patient?_count=10", timeout=10)  # Added timeout
    if response.status_code == 200:
        patients = response.json().get('entry', [])
        processed_patients = process_patient_results(patients)
        return render_template('patients.html', patients=processed_patients)
    else:
        return jsonify({"error": "Failed to fetch patients"}), 500

@app.route('/fhir/search_patients', methods=['POST'])
@login_required
def search_patients():
    search_term = request.form.get('q', '').strip().lower()
    
    if not search_term:
        # If search is empty, return all patients
        return get_patients_table_body()
    
    # Search patients by name or identifier
    try:
        response = requests.get(f"{FHIR_SERVER}/Patient?_count=20&name:contains={search_term}", timeout=10)
        
        if response.status_code == 200:
            patients = response.json().get('entry', [])
            processed_patients = process_patient_results(patients)
            return render_template('patient_table_body.html', patients=processed_patients)
        else:
            logging.error(f"API error: {response.status_code} when searching patients")
            return "<tr><td colspan='7'>Error searching patients</td></tr>", 500
    except requests.exceptions.RequestException as e:
        logging.error(f"Request failed: {str(e)}")
        return "<tr><td colspan='7'>Connection error, please try again</td></tr>", 500

@app.route('/fhir/Patient/<patient_id>')
@login_required
def get_patient(patient_id):
    response = requests.get(f"{FHIR_SERVER}/Patient/{patient_id}")
    if response.status_code == 200:
        patient = response.json()

        # Simplify work phone numbers
        work_telecoms = [telecom['value'] for telecom in patient.get('telecom', []) if telecom.get('use') == 'work']
        patient['work_phone'] = ', '.join(work_telecoms)

        # Simplify the address
        if patient.get('address'):
            address_parts = patient['address'][0].get('line', [])
            address_parts.extend([
                patient['address'][0].get('city', ''),
                patient['address'][0].get('state', ''),
                patient['address'][0].get('postalCode', '')
            ])
            patient['simple_address'] = ', '.join(filter(None, address_parts))  # Filter out empty strings

        # Simplify identifier, assume the first identifier is the most relevant
        if patient.get('identifier'):
            patient['simple_identifier'] = patient['identifier'][0].get('value', '')
            
        # Extract communication languages
        patient['languages'] = []
        if patient.get('communication'):
            for comm in patient['communication']:
                if 'language' in comm:
                    language_text = comm['language'].get('text', '')
                    if not language_text and 'coding' in comm['language']:
                        language_text = comm['language']['coding'][0].get('display', '')
                    if language_text:
                        preferred = 'Preferred' if comm.get('preferred', False) else ''
                        patient['languages'].append({'name': language_text, 'preferred': preferred})

        # Process age from birth date
        if patient.get('birthDate'):
            try:
                birth_date = datetime.strptime(patient['birthDate'], '%Y-%m-%d')
                today = datetime.now()
                age = today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))
                patient['age'] = age
            except ValueError:
                patient['age'] = 'Unknown'

        return render_template('patient_details.html', patient=patient)
    else:
        return jsonify({"error": "Failed to fetch patient details"}), 500

@app.route('/fhir/LabResults/<patient_id>')
@login_required
def get_lab_results(patient_id):
    response = requests.get(f"{FHIR_SERVER}/Observation?patient={patient_id}&_sort=-date&_count=5")
    if response.status_code == 200:
        lab_results = response.json().get('entry', [])
        # Prepare lab results for rendering
        for result in lab_results:
            effectiveDateTime = result['resource'].get('effectiveDateTime', '')
            formatted_date = datetime.strptime(effectiveDateTime[:-6], '%Y-%m-%dT%H:%M:%S').strftime('%Y-%m-%d')
            result['resource']['formattedDate'] = formatted_date
        # Render the results into HTML using the lab_results template
        return render_template('lab_results.html', lab_results=lab_results)
    else:
        return "Lab results not found", 404

@app.route('/fhir/VitalSigns/<patient_id>')
@login_required
def get_vital_signs(patient_id):
    # First, get blood pressure observations
    bp_response = requests.get(f"{FHIR_SERVER}/Observation?patient={patient_id}&code=http://loinc.org|85354-9&_sort=-date&_count=10")
    
    # Get heart rate observations
    hr_response = requests.get(f"{FHIR_SERVER}/Observation?patient={patient_id}&code=http://loinc.org|8867-4&_sort=-date&_count=10")
    
    # Get temperature observations
    temp_response = requests.get(f"{FHIR_SERVER}/Observation?patient={patient_id}&code=http://loinc.org|8310-5&_sort=-date&_count=10")
    
    # Get respiratory rate observations
    resp_response = requests.get(f"{FHIR_SERVER}/Observation?patient={patient_id}&code=http://loinc.org|9279-1&_sort=-date&_count=10")
    
    vital_signs = []
    
    # Process blood pressure readings
    if bp_response.status_code == 200:
        bp_data = bp_response.json().get('entry', [])
        for entry in bp_data:
            resource = entry.get('resource', {})
            
            # Extract the date
            effective_date = resource.get('effectiveDateTime', '')
            if effective_date:
                try:
                    formatted_date = datetime.strptime(effective_date[:-6], '%Y-%m-%dT%H:%M:%S').strftime('%Y-%m-%d')
                except (ValueError, TypeError):
                    formatted_date = 'Unknown Date'
            else:
                formatted_date = 'Unknown Date'
                
            # Extract components (systolic/diastolic)
            components = resource.get('component', [])
            systolic = None
            diastolic = None
            
            for component in components:
                coding = component.get('code', {}).get('coding', [{}])
                if coding and coding[0].get('display') == 'Systolic blood pressure':
                    systolic = component.get('valueQuantity', {}).get('value')
                elif coding and coding[0].get('display') == 'Diastolic blood pressure':
                    diastolic = component.get('valueQuantity', {}).get('value')
            
            # Only add if we have both systolic and diastolic
            if systolic is not None and diastolic is not None:
                vital_signs.append({
                    'date': formatted_date,
                    'type': 'Blood Pressure',
                    'value': f'{systolic}/{diastolic}',
                    'unit': 'mmHg',
                    'status': resource.get('status', 'unknown'),
                    'components': {
                        'systolic': systolic,
                        'diastolic': diastolic
                    }
                })
    
    # Process heart rate readings
    if hr_response.status_code == 200:
        hr_data = hr_response.json().get('entry', [])
        for entry in hr_data:
            resource = entry.get('resource', {})
            
            # Extract the date
            effective_date = resource.get('effectiveDateTime', '')
            if effective_date:
                try:
                    formatted_date = datetime.strptime(effective_date[:-6], '%Y-%m-%dT%H:%M:%S').strftime('%Y-%m-%d')
                except (ValueError, TypeError):
                    formatted_date = 'Unknown Date'
            else:
                formatted_date = 'Unknown Date'
                
            # Get heart rate value
            value_quantity = resource.get('valueQuantity', {})
            value = value_quantity.get('value')
            
            # Make sure value is a number
            try:
                value = float(value)
            except (ValueError, TypeError):
                value = 0
            
            vital_signs.append({
                'date': formatted_date,
                'type': 'Heart Rate',
                'value': value,
                'unit': value_quantity.get('unit', 'bpm'),
                'status': resource.get('status', 'unknown')
            })
    
    # Process temperature readings
    if temp_response.status_code == 200:
        temp_data = temp_response.json().get('entry', [])
        for entry in temp_data:
            resource = entry.get('resource', {})
            
            # Extract the date
            effective_date = resource.get('effectiveDateTime', '')
            if effective_date:
                try:
                    formatted_date = datetime.strptime(effective_date[:-6], '%Y-%m-%dT%H:%M:%S').strftime('%Y-%m-%d')
                except (ValueError, TypeError):
                    formatted_date = 'Unknown Date'
            else:
                formatted_date = 'Unknown Date'
                
            # Get temperature value
            value_quantity = resource.get('valueQuantity', {})
            
            vital_signs.append({
                'date': formatted_date,
                'type': 'Temperature',
                'value': value_quantity.get('value', 'Unknown'),
                'unit': value_quantity.get('unit', '°C'),
                'status': resource.get('status', 'unknown')
            })
    
    # Process respiratory rate readings
    if resp_response.status_code == 200:
        resp_data = resp_response.json().get('entry', [])
        for entry in resp_data:
            resource = entry.get('resource', {})
            
            # Extract the date
            effective_date = resource.get('effectiveDateTime', '')
            if effective_date:
                try:
                    formatted_date = datetime.strptime(effective_date[:-6], '%Y-%m-%dT%H:%M:%S').strftime('%Y-%m-%d')
                except (ValueError, TypeError):
                    formatted_date = 'Unknown Date'
            else:
                formatted_date = 'Unknown Date'
                
            # Get respiratory rate value
            value_quantity = resource.get('valueQuantity', {})
            
            vital_signs.append({
                'date': formatted_date,
                'type': 'Respiratory Rate',
                'value': value_quantity.get('value', 'Unknown'),
                'unit': value_quantity.get('unit', '/min'),
                'status': resource.get('status', 'unknown')
            })
    
    # Sort vital signs by date (newest first)
    vital_signs.sort(key=lambda x: x['date'], reverse=True)
    
    return render_template('vital_signs.html', vital_signs=vital_signs)

@app.route('/fhir/Medications/<patient_id>')
@login_required
def get_medications(patient_id):
    # Get medications
    meds_response = requests.get(f"{FHIR_SERVER}/MedicationRequest?patient={patient_id}&_count=10")
    
    medications = []
    
    if meds_response.status_code == 200:
        meds_data = meds_response.json()
        # Check if 'entry' exists in the response
        if 'entry' in meds_data:
            for entry in meds_data.get('entry', []):
                resource = entry.get('resource', {})
                
                # Extract the date
                authored_date = resource.get('authoredOn', '')
                if authored_date:
                    try:
                        formatted_date = datetime.strptime(authored_date[:10], '%Y-%m-%d').strftime('%Y-%m-%d')
                    except (ValueError, TypeError):
                        formatted_date = 'Unknown Date'
                else:
                    formatted_date = 'Unknown Date'
                
                # Extract medication reference or codeable concept
                medication_name = "Unknown Medication"
                if resource.get('medicationReference'):
                    medication_name = resource['medicationReference'].get('display', 'Unknown Medication')
                elif resource.get('medicationCodeableConcept'):
                    coding = resource['medicationCodeableConcept'].get('coding', [{}])
                    if coding:
                        medication_name = coding[0].get('display', 'Unknown Medication')
                    else:
                        medication_name = resource['medicationCodeableConcept'].get('text', 'Unknown Medication')
                
                # Get dosage instructions
                dosage_instructions = []
                if resource.get('dosageInstruction'):
                    for dosage in resource['dosageInstruction']:
                        text = dosage.get('text', '')
                        if text:
                            dosage_instructions.append(text)
                
                # Get status
                status = resource.get('status', 'unknown')
                
                medications.append({
                    'date': formatted_date,
                    'name': medication_name,
                    'dosage': ', '.join(dosage_instructions) if dosage_instructions else 'No specific instructions',
                    'status': status
                })
        
        # Log if no medications found
        if not medications:
            logging.info(f"No medications found for patient {patient_id}")
    
    # Sort medications by date (newest first)
    medications.sort(key=lambda x: x['date'], reverse=True)
    
    return render_template('medications.html', medications=medications)

@app.route('/fhir/Allergies/<patient_id>')
@login_required
def get_allergies(patient_id):
    # Get allergies
    allergy_response = requests.get(f"{FHIR_SERVER}/AllergyIntolerance?patient={patient_id}&_count=10")
    
    allergies = []
    
    if allergy_response.status_code == 200:
        allergy_data = allergy_response.json()
        # Check if 'entry' exists in the response
        if 'entry' in allergy_data:
            for entry in allergy_data.get('entry', []):
                resource = entry.get('resource', {})
                
                # Extract the date
                recorded_date = resource.get('recordedDate', '')
                if recorded_date:
                    try:
                        formatted_date = datetime.strptime(recorded_date[:10], '%Y-%m-%d').strftime('%Y-%m-%d')
                    except (ValueError, TypeError):
                        formatted_date = 'Unknown Date'
                else:
                    formatted_date = 'Unknown Date'
                
                # Extract allergy code or display
                allergy_name = "Unknown Allergen"
                if resource.get('code'):
                    coding = resource['code'].get('coding', [{}])
                    if coding:
                        allergy_name = coding[0].get('display', 'Unknown Allergen')
                    else:
                        allergy_name = resource['code'].get('text', 'Unknown Allergen')
                
                # Get reaction
                reactions = []
                if resource.get('reaction'):
                    for reaction in resource['reaction']:
                        if reaction.get('manifestation'):
                            for manifestation in reaction['manifestation']:
                                if manifestation.get('coding'):
                                    reactions.append(manifestation['coding'][0].get('display', ''))
                                elif manifestation.get('text'):
                                    reactions.append(manifestation['text'])
                
                # Get status
                clinical_status = "Unknown"
                if resource.get('clinicalStatus'):
                    if resource['clinicalStatus'].get('coding'):
                        clinical_status = resource['clinicalStatus']['coding'][0].get('display', 'Unknown')
                    else:
                        clinical_status = resource['clinicalStatus'].get('text', 'Unknown')
                
                # Get severity
                severity = resource.get('severity', 'unknown')
                
                allergies.append({
                    'date': formatted_date,
                    'name': allergy_name,
                    'reactions': reactions,
                    'severity': severity,
                    'status': clinical_status
                })
        
        # Log if no allergies found
        if not allergies:
            logging.info(f"No allergies found for patient {patient_id}")
    
    # Sort allergies by date (newest first)
    allergies.sort(key=lambda x: x['date'], reverse=True)
    
    return render_template('allergies.html', allergies=allergies)

@app.route('/fhir/Demographics')
@login_required
def get_demographics():
    """Get patient demographics statistics for visualization"""
    # Fetch patients for demographic analysis
    response = requests.get(f"{FHIR_SERVER}/Patient?_count=100")
    
    if response.status_code == 200:
        patients = response.json().get('entry', [])
        
        # Initialize counters
        gender_counts = {"male": 0, "female": 0, "other": 0, "unknown": 0}
        age_groups = {"0-18": 0, "19-35": 0, "36-55": 0, "56-75": 0, "76+": 0, "unknown": 0}
        
        current_year = datetime.now().year
        
        # Process each patient
        for patient_entry in patients:
            resource = patient_entry.get('resource', {})
            
            # Count genders
            gender = resource.get('gender', '').lower()
            if gender in gender_counts:
                gender_counts[gender] += 1
            else:
                gender_counts["unknown"] += 1
                
            # Determine age group
            birth_date = resource.get('birthDate')
            if birth_date and len(birth_date) >= 4:
                try:
                    birth_year = int(birth_date[:4])
                    age = current_year - birth_year
                    
                    if age <= 18:
                        age_groups["0-18"] += 1
                    elif age <= 35:
                        age_groups["19-35"] += 1
                    elif age <= 55:
                        age_groups["36-55"] += 1
                    elif age <= 75:
                        age_groups["56-75"] += 1
                    else:
                        age_groups["76+"] += 1
                except ValueError:
                    age_groups["unknown"] += 1
            else:
                age_groups["unknown"] += 1
        
        # Calculate total for percentages
        total_patients = sum(gender_counts.values())
        
        demographics = {
            "gender_counts": gender_counts,
            "age_groups": age_groups,
            "total_patients": total_patients
        }
        
        return render_template('demographics.html', demographics=demographics)
    else:
        return jsonify({"error": "Failed to fetch patient demographics"}), 500

@app.route('/fhir/Dashboard')
@login_required
def get_dashboard():
    """Get dashboard data for the main dashboard view"""
    # Fetch patients
    patient_response = requests.get(f"{FHIR_SERVER}/Patient?_count=100")
    
    # Fetch observations count
    observation_response = requests.get(f"{FHIR_SERVER}/Observation?_summary=count")
    
    patient_count = 0
    observation_count = 0
    gender_counts = {"male": 0, "female": 0, "other": 0, "unknown": 0}
    recent_patients = []
    
    if patient_response.status_code == 200:
        patient_data = patient_response.json()
        patients = patient_data.get('entry', [])
        patient_count = len(patients)
        
        # Process gender counts
        for patient_entry in patients:
            resource = patient_entry.get('resource', {})
            
            # Count genders
            gender = resource.get('gender', '').lower()
            if gender in gender_counts:
                gender_counts[gender] += 1
            else:
                gender_counts["unknown"] += 1
                
        # Get recent patients
        recent_patients_data = patients[:5]  # Just take the first 5 for simplicity
        recent_patients = process_patient_results(recent_patients_data)
    
    if observation_response.status_code == 200:
        observation_data = observation_response.json()
        observation_count = observation_data.get('total', 0)
    
    dashboard_data = {
        'patient_count': patient_count,
        'observation_count': observation_count,
        'gender_counts': gender_counts,
        'recent_patients': recent_patients,
        'fhir_server_url': FHIR_SERVER
    }
    
    return render_template('dashboard.html', **dashboard_data)

@app.route('/settings/ai')
@login_required
def ai_settings():
    return render_template('ai_settings.html')

@app.route('/test_openai_key', methods=['POST'])
@login_required
def test_openai_key():
    api_key = request.json.get('api_key')
    if not api_key:
        return jsonify({'error': 'API key is required'}), 400

    try:
        # It's good practice to instantiate the client for each request that needs it,
        # especially if the API key can change or if you want to ensure thread safety
        # or specific configurations per request.
        client = openai.OpenAI(api_key=api_key)

        # Make a simple, low-cost API call to test the key
        client.chat.completions.create(
            model="gpt-4.1-nano-2025-04-14", # Specified model
            messages=[{"role": "user", "content": "test"}]
        )
        return jsonify({'success': True, 'message': 'API key is valid.'})
    except openai.AuthenticationError:
        return jsonify({'success': False, 'message': 'API key is invalid or expired.'}), 401
    except openai.APIError as e:
        # Handle more specific API errors if needed
        return jsonify({'success': False, 'message': f'OpenAI API error: {str(e)}'}), 500
    except Exception as e:
        # Catch any other unexpected errors
        logging.error(f"Unexpected error during OpenAI key test: {str(e)}")
        return jsonify({'success': False, 'message': f'An unexpected error occurred: {str(e)}'}), 500

@app.route('/get_nonce_to_sign/<wallet_address>')
def get_nonce_to_sign(wallet_address):
    if not wallet_address:
        return jsonify({'error': 'Wallet address is required'}), 400

    # Basic validation for wallet address format (optional but good)
    if not (wallet_address.startswith('0x') and len(wallet_address) == 42):
        return jsonify({'error': 'Invalid wallet address format'}), 400

    # Generate a more human-readable nonce for signing
    nonce_bytes = os.urandom(16)
    nonce = f"Sign this message to log in: {binascii.hexlify(nonce_bytes).decode()}"
    session[f'nonce_{wallet_address.lower()}'] = nonce # Store nonce with lowercase address
    logging.info(f"Generated nonce for address {wallet_address}: {nonce}")
    return jsonify({'nonce': nonce})

@app.route('/verify_signature', methods=['POST'])
def verify_signature():
    data = request.json
    signed_message = data.get('signed_message')
    original_nonce = data.get('original_nonce')
    wallet_address = data.get('wallet_address')

    if not all([signed_message, original_nonce, wallet_address]):
        return jsonify({'success': False, 'error': 'Missing required parameters.'}), 400

    stored_nonce_key = f'nonce_{wallet_address.lower()}'
    stored_nonce = session.get(stored_nonce_key)

    if not stored_nonce:
        logging.warning(f"No nonce found in session for address {wallet_address}")
        return jsonify({'success': False, 'error': 'Nonce not found in session or session expired.'}), 400

    if original_nonce != stored_nonce:
        logging.warning(f"Nonce mismatch for address {wallet_address}. Given: {original_nonce}, Stored: {stored_nonce}")
        return jsonify({'success': False, 'error': 'Nonce mismatch. Possible replay attack.'}), 400

    # Nonce verified, remove from session
    session.pop(stored_nonce_key, None)

    try:
        # Use encode_defunct treating original_nonce as the text that was signed
        signable_message = encode_defunct(text=original_nonce)
        recovered_address = Account.recover_message(signable_message, signature=signed_message)

        if recovered_address.lower() == wallet_address.lower():
            user = User(id=wallet_address.lower()) # Use lowercase for consistency
            login_user(user)
            logging.info(f"Successfully verified signature for address {wallet_address}. User logged in.")
            return jsonify({'success': True, 'message': 'Signature verified. User logged in.'})
        else:
            logging.warning(f"Signature verification failed for address {wallet_address}. Recovered: {recovered_address.lower()}")
            return jsonify({'success': False, 'error': 'Signature verification failed.'}), 401
    except Exception as e: # Catching broad exceptions, consider more specific ones like BadSignature
        logging.error(f"Error during signature recovery for {wallet_address}: {str(e)}")
        return jsonify({'success': False, 'error': f'Invalid signature or recovery failed: {str(e)}'}), 400

@app.route('/logout')
@login_required # Ensure user is logged in to log out
def logout():
    logout_user()
    logging.info("User logged out.")
    return redirect(url_for('index'))

@app.route('/login')
def login():
    # Show dedicated login page for unauthenticated users
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/summarize_patient/<patient_id>')
@login_required
def summarize_patient(patient_id):
    # Attempt to get OpenAI API key from environment variable
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        logging.error("OPENAI_API_KEY not found in environment variables")
        return jsonify({"error": "OpenAI API key is not configured on the server."}), 500

    # Initialize OpenAI client
    try:
        client = openai.OpenAI(api_key=api_key)
    except Exception as e:
        logging.error(f"Failed to initialize OpenAI client: {str(e)}")
        return jsonify({"error": "Failed to initialize AI service client. Please contact support."}), 500

    # Fetch patient data
    try:
        fhir_response = requests.get(f"{FHIR_SERVER}/Patient/{patient_id}", timeout=10)
        fhir_response.raise_for_status() # Raises an HTTPError for bad responses (4xx or 5xx)
    except requests.exceptions.HTTPError as e:
        logging.error(f"FHIR API error when fetching patient {patient_id}: {str(e)}")
        return jsonify({"error": f"Failed to fetch patient data from FHIR server: {e.response.status_code} {e.response.reason}"}), e.response.status_code
    except requests.exceptions.RequestException as e:
        logging.error(f"Request exception when fetching patient {patient_id}: {str(e)}")
        return jsonify({"error": "Failed to connect to FHIR server. Please try again later."}), 503 # Service Unavailable

    patient_data = fhir_response.json()

    # Construct a simplified patient summary for the prompt
    # This can be expanded to include more details as needed
    name_data = patient_data.get('name', [{}])[0]
    patient_name = f"{' '.join(name_data.get('given', []))} {name_data.get('family', '')}".strip()
    gender = patient_data.get('gender', 'Unknown')
    birth_date = patient_data.get('birthDate', 'Unknown')

    # Simplified information string
    patient_info_for_prompt = (
        f"Patient Name: {patient_name}\n"
        f"Gender: {gender}\n"
        f"Birth Date: {birth_date}\n"
    )

    # Add conditions if available - simplified example
    conditions_response = requests.get(f"{FHIR_SERVER}/Condition?patient={patient_id}&_count=5")
    if conditions_response.status_code == 200:
        conditions_data = conditions_response.json().get('entry', [])
        if conditions_data:
            patient_info_for_prompt += "Key Conditions:\n"
            for entry in conditions_data:
                condition_text = entry.get('resource', {}).get('code', {}).get('text', 'Unknown condition')
                patient_info_for_prompt += f"- {condition_text}\n"

    # Construct the prompt for OpenAI
    prompt = (
        "Please provide a concise summary of the key information for the following patient, "
        "highlighting critical aspects that a healthcare provider should be aware of. "
        "Focus on actionable insights if possible.\n\n"
        f"{patient_info_for_prompt}"
    )

    try:
        # Call OpenAI API
        chat_completion = client.chat.completions.create(
            model="gpt-4.1-nano-2025-04-14",
            messages=[
                {"role": "system", "content": "You are a helpful medical assistant that summarizes patient data."},
                {"role": "user", "content": prompt}
            ]
        )
        summary = chat_completion.choices[0].message.content
        return jsonify({"patient_id": patient_id, "summary": summary})

    except openai.AuthenticationError as e:
        logging.error(f"OpenAI AuthenticationError: {str(e)}")
        return jsonify({"error": "AI service authentication failed. Please check the configured API key."}), 401
    except openai.APIError as e:
        logging.error(f"OpenAI APIError: {str(e)}")
        return jsonify({"error": "Failed to get summary from AI service due to an API error. Please try again later."}), 500
    except Exception as e:
        logging.error(f"An unexpected error occurred while generating summary for patient {patient_id}: {str(e)}")
        return jsonify({"error": "An unexpected error occurred while communicating with the AI service."}), 500

if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(debug=debug_mode)