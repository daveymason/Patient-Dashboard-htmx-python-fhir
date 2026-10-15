"""File-backed Local FHIR R4 Store for Slainte-Engine.

Indexes and serves FHIR R4 JSON bundles from the persistent storage
directory (/workspace/data/fhir_store), providing seamless zero-latency
access for the HTMX dashboard without requiring a heavy external FHIR server.
"""
import glob
import json
import logging
import os
import time
from typing import Dict, Any, List, Optional
from fhir_client import FhirResult

logger = logging.getLogger(__name__)

class LocalFhirStore:
    def __init__(self, store_path: str = "/workspace/data/fhir_store"):
        self.store_path = store_path
        try:
            os.makedirs(self.store_path, exist_ok=True)
        except OSError:
            self.store_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data', 'fhir_store'))
            os.makedirs(self.store_path, exist_ok=True)
        self._last_scan_time = 0.0
        self._cache_patients: Dict[str, Dict[str, Any]] = {}
        self._cache_observations: Dict[str, List[Dict[str, Any]]] = {} # pid -> list
        self._cache_conditions: Dict[str, List[Dict[str, Any]]] = {}
        self._cache_encounters: Dict[str, List[Dict[str, Any]]] = {}
        self._cache_medications: Dict[str, List[Dict[str, Any]]] = {}
        self._cache_allergies: Dict[str, List[Dict[str, Any]]] = {}
        self._cache_bundles: Dict[str, Dict[str, Any]] = {}
        self._file_mtimes: Dict[str, float] = {}

    def _refresh_if_needed(self):
        """Scans the directory for newly written or modified bundles."""
        now = time.time()
        # Scan at most once per second
        if now - self._last_scan_time < 1.0 and self._cache_patients:
            return

        json_files = glob.glob(os.path.join(self.store_path, "*.json"))
        changed = False

        for fpath in json_files:
            try:
                mtime = os.path.getmtime(fpath)
                if fpath not in self._file_mtimes or self._file_mtimes[fpath] < mtime:
                    self._file_mtimes[fpath] = mtime
                    changed = True
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self._index_bundle(data, fpath)
            except Exception as e:
                logger.warning(f"Failed to read FHIR file {fpath}: {e}")

        self._last_scan_time = now

    def _index_bundle(self, bundle: Dict[str, Any], source_file: str):
        if not isinstance(bundle, dict):
            return
        
        rtype = bundle.get("resourceType")
        entries = []
        if rtype == "Bundle":
            entries = bundle.get("entry", [])
        else:
            entries = [{"resource": bundle}]

        current_patient_id = None
        # First find Patient
        for e in entries:
            res = e.get("resource", {}) if isinstance(e, dict) else {}
            if res.get("resourceType") == "Patient":
                pid = str(res.get("id"))
                current_patient_id = pid
                self._cache_patients[pid] = res
                self._cache_bundles[pid] = bundle
                if pid not in self._cache_observations:
                    self._cache_observations[pid] = []
                if pid not in self._cache_conditions:
                    self._cache_conditions[pid] = []
                if pid not in self._cache_encounters:
                    self._cache_encounters[pid] = []
                if pid not in self._cache_medications:
                    self._cache_medications[pid] = []
                if pid not in self._cache_allergies:
                    self._cache_allergies[pid] = []

        # Index other resources
        for e in entries:
            res = e.get("resource", {}) if isinstance(e, dict) else {}
            rt = res.get("resourceType")
            subj_ref = ""
            if isinstance(res.get("subject"), dict):
                subj_ref = res["subject"].get("reference", "")
            
            target_pid = current_patient_id
            if subj_ref.startswith("Patient/"):
                target_pid = subj_ref.split("/")[-1]

            if not target_pid and self._cache_patients:
                target_pid = list(self._cache_patients.keys())[-1]

            if target_pid:
                if rt == "Observation":
                    # Avoid duplicates
                    obs_id = res.get("id")
                    existing = [o for o in self._cache_observations.get(target_pid, []) if o.get("id") == obs_id]
                    if not existing:
                        self._cache_observations.setdefault(target_pid, []).append(res)
                elif rt == "Condition":
                    cond_id = res.get("id")
                    existing = [c for c in self._cache_conditions.get(target_pid, []) if c.get("id") == cond_id]
                    if not existing:
                        self._cache_conditions.setdefault(target_pid, []).append(res)
                elif rt == "Encounter":
                    enc_id = res.get("id")
                    existing = [x for x in self._cache_encounters.get(target_pid, []) if x.get("id") == enc_id]
                    if not existing:
                        self._cache_encounters.setdefault(target_pid, []).append(res)
                elif rt == "MedicationRequest":
                    med_id = res.get("id")
                    existing = [m for m in self._cache_medications.get(target_pid, []) if m.get("id") == med_id]
                    if not existing:
                        self._cache_medications.setdefault(target_pid, []).append(res)
                elif rt == "AllergyIntolerance":
                    alg_id = res.get("id")
                    existing = [a for a in self._cache_allergies.get(target_pid, []) if a.get("id") == alg_id]
                    if not existing:
                        self._cache_allergies.setdefault(target_pid, []).append(res)

    def save_bundle(self, bundle: Dict[str, Any], filename: Optional[str] = None) -> str:
        """Saves bundle to disk and immediately indexes it."""
        pid = "unknown"
        if bundle.get("entry"):
            for e in bundle["entry"]:
                r = e.get("resource", {})
                if r.get("resourceType") == "Patient" and r.get("id"):
                    pid = str(r["id"])
                    break
        raw_fname = filename or f"bundle_{pid}_{int(time.time())}.json"
        fname = os.path.basename(raw_fname)
        target_path = os.path.join(self.store_path, fname)
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(bundle, f, indent=2)
        self._index_bundle(bundle, target_path)
        return target_path

    # --- FhirClient Interface Implementation ---

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> FhirResult:
        self._refresh_if_needed()
        clean_path = path.strip("/")
        parts = clean_path.split("/")
        resource_type = parts[0]
        resource_id = parts[1] if len(parts) > 1 else None
        params = params or {}

        # 1. Single Resource Get: e.g. Patient/123
        if resource_id:
            if resource_type == "Patient":
                patient = self._cache_patients.get(resource_id)
                if patient:
                    return FhirResult(True, data=patient, status_code=200)
                return FhirResult(False, status_code=404, error=f"Patient {resource_id} not found in store.")
            
            # Lookup Observation by ID
            if resource_type == "Observation":
                for obs_list in self._cache_observations.values():
                    for obs in obs_list:
                        if obs.get("id") == resource_id:
                            return FhirResult(True, data=obs, status_code=200)
                return FhirResult(False, status_code=404, error=f"Observation {resource_id} not found.")

        # 2. Search / List: Patient
        if resource_type == "Patient":
            patients = list(self._cache_patients.values())
            # Handle query search
            name_q = params.get("name:contains") or params.get("name")
            id_q = params.get("_id") or params.get("id")
            ident_q = params.get("identifier")

            if id_q:
                patients = [p for p in patients if str(p.get("id")) == str(id_q)]
            elif ident_q:
                filtered = []
                for p in patients:
                    for ident in p.get("identifier", []):
                        if ident_q in str(ident.get("value", "")):
                            filtered.append(p)
                            break
                patients = filtered
            elif name_q:
                q_lower = name_q.lower()
                filtered = []
                for p in patients:
                    found = False
                    for nm in p.get("name", []):
                        txt = nm.get("text", "") or ""
                        fam = nm.get("family", "") or ""
                        giv = " ".join(nm.get("given", []))
                        if q_lower in txt.lower() or q_lower in fam.lower() or q_lower in giv.lower():
                            found = True
                            break
                    if found:
                        filtered.append(p)
                patients = filtered

            # Sort
            sort_field = params.get("_sort")
            if sort_field:
                reverse = sort_field.startswith("-")
                field = sort_field.lstrip("-")
                if field == "family":
                    patients.sort(key=lambda p: (p.get("name", [{}])[0].get("family") or ""), reverse=reverse)
                elif field == "birthdate":
                    patients.sort(key=lambda p: (p.get("birthDate") or ""), reverse=reverse)

            count = int(params.get("_count", 20))
            page = patients[:count]
            bundle = {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": len(patients),
                "entry": [{"resource": p} for p in page]
            }
            return FhirResult(True, data=bundle, status_code=200)

        # 3. Observation search
        if resource_type == "Observation":
            target_pid = params.get("patient") or params.get("subject")
            if target_pid and target_pid.startswith("Patient/"):
                target_pid = target_pid.split("/")[-1]
            
            obs_list = []
            if target_pid:
                obs_list = self._cache_observations.get(str(target_pid), [])
            else:
                for lst in self._cache_observations.values():
                    obs_list.extend(lst)

            # Filter by category (e.g. 'laboratory', 'vital-signs')
            category_filter = params.get("category")
            if category_filter:
                cat_target = category_filter.lower().split("|")[-1].strip()
                filtered_by_cat = []
                vital_loincs = {"8480-6", "8462-4", "85354-9", "8867-4", "59408-5", "9279-1", "8310-5", "72514-3"}
                for o in obs_list:
                    cats = o.get("category", [])
                    cat_codes = []
                    for c in cats:
                        for cd in c.get("coding", []):
                            if cd.get("code"):
                                cat_codes.append(cd["code"].lower())
                    obs_loincs = {c.get("code") for c in o.get("code", {}).get("coding", [])}
                    is_vital = bool(obs_loincs & vital_loincs)
                    
                    if cat_target in cat_codes:
                        filtered_by_cat.append(o)
                    elif not cat_codes:
                        if cat_target == "vital-signs" and is_vital:
                            filtered_by_cat.append(o)
                        elif cat_target == "laboratory" and not is_vital and "75540-5" not in obs_loincs:
                            filtered_by_cat.append(o)
                obs_list = filtered_by_cat

            # Filter by code
            code_filter = params.get("code")
            if code_filter:
                raw_codes = [c.strip() for c in code_filter.split(",")]
                codes_to_match = set()
                for c in raw_codes:
                    codes_to_match.add(c)
                    if "|" in c:
                        codes_to_match.add(c.split("|")[-1])
                matched = []
                for o in obs_list:
                    cd = o.get("code", {})
                    obs_codes = [c.get("code") for c in cd.get("coding", [])]
                    if cd.get("text") in codes_to_match or any(c in codes_to_match for c in obs_codes):
                        matched.append(o)
                obs_list = matched

            bundle = {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": len(obs_list),
                "entry": [{"resource": o} for o in obs_list]
            }
            return FhirResult(True, data=bundle, status_code=200)

        # 4. Condition search
        if resource_type == "Condition":
            target_pid = params.get("patient") or params.get("subject")
            if target_pid and target_pid.startswith("Patient/"):
                target_pid = target_pid.split("/")[-1]
            conds = self._cache_conditions.get(str(target_pid), []) if target_pid else []
            bundle = {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": len(conds),
                "entry": [{"resource": c} for c in conds]
            }
            return FhirResult(True, data=bundle, status_code=200)

        # 5. Encounter search
        if resource_type == "Encounter":
            target_pid = params.get("patient") or params.get("subject")
            if target_pid and target_pid.startswith("Patient/"):
                target_pid = target_pid.split("/")[-1]
            encs = self._cache_encounters.get(str(target_pid), []) if target_pid else []
            bundle = {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": len(encs),
                "entry": [{"resource": e} for e in encs]
            }
            return FhirResult(True, data=bundle, status_code=200)

        # 6. MedicationRequest search
        if resource_type == "MedicationRequest":
            target_pid = params.get("patient") or params.get("subject")
            if target_pid and target_pid.startswith("Patient/"):
                target_pid = target_pid.split("/")[-1]
            meds = self._cache_medications.get(str(target_pid), []) if target_pid else []
            bundle = {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": len(meds),
                "entry": [{"resource": m} for m in meds]
            }
            return FhirResult(True, data=bundle, status_code=200)

        # 7. AllergyIntolerance search
        if resource_type == "AllergyIntolerance":
            target_pid = params.get("patient") or params.get("subject")
            if target_pid and target_pid.startswith("Patient/"):
                target_pid = target_pid.split("/")[-1]
            allergies = self._cache_allergies.get(str(target_pid), []) if target_pid else []
            bundle = {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": len(allergies),
                "entry": [{"resource": a} for a in allergies]
            }
            return FhirResult(True, data=bundle, status_code=200)

        # 6. Metadata probe
        if resource_type == "metadata":
            return FhirResult(True, data={"resourceType": "CapabilityStatement", "status": "active"}, status_code=200)

        return FhirResult(True, data={"resourceType": "Bundle", "type": "searchset", "total": 0, "entry": []}, status_code=200)

    def get_absolute(self, url: str) -> FhirResult:
        return self.get("Patient")

    def get_many(self, requests_spec: Dict[str, tuple]) -> Dict[str, FhirResult]:
        return {key: self.get(path, params) for key, (path, params) in requests_spec.items()}

    def count(self, resource_type: str, params: Optional[Dict[str, Any]] = None) -> Optional[int]:
        self._refresh_if_needed()
        if resource_type == "Patient":
            return len(self._cache_patients)
        if resource_type == "Observation":
            return sum(len(l) for l in self._cache_observations.values())
        return 0

    def capability_ok(self) -> bool:
        return os.path.exists(self.store_path)

    def get_triage_queue(self) -> List[Dict[str, Any]]:
        """
        Calculates and returns the Live Triage Queue,
        sorted strictly by Manchester Triage System Category (1 Immediate -> 5 Non-Urgent).
        """
        self._refresh_if_needed()
        queue = []

        MTS_META = {
            1: {"name": "Immediate", "color": "danger", "target": "0 min", "badge": "bg-danger text-white"},
            2: {"name": "Very Urgent", "color": "warning", "target": "10 min", "badge": "bg-warning text-dark"},
            3: {"name": "Urgent", "color": "info", "target": "60 min", "badge": "bg-info text-dark"},
            4: {"name": "Standard", "color": "primary", "target": "120 min", "badge": "bg-primary text-white"},
            5: {"name": "Non-Urgent", "color": "secondary", "target": "240 min", "badge": "bg-secondary text-white"}
        }

        for pid, patient in self._cache_patients.items():
            obs_list = self._cache_observations.get(pid, [])
            cond_list = self._cache_conditions.get(pid, [])
            enc_list = self._cache_encounters.get(pid, [])

            # Extract Patient Name
            names = patient.get("name", [])
            display_name = f"Patient {pid}"
            if names:
                display_name = names[0].get("text") or f"{names[0].get('given', [''])[0]} {names[0].get('family', '')}".strip()

            # Extract Irish GMS Identifier
            gms_number = "GMS-UNKNOWN"
            for ident in patient.get("identifier", []):
                if ident.get("value"):
                    gms_number = ident.get("value")
                    break

            # Find MTS Category Observation
            mts_category = 3  # default urgent
            for o in obs_list:
                cd = o.get("code", {})
                codes = [c.get("code") for c in cd.get("coding", [])]
                text = cd.get("text", "")
                if "75540-5" in codes or "Manchester Triage System" in text or "MTS" in text:
                    val = o.get("valueQuantity", {}).get("value")
                    if val is not None:
                        try:
                            mts_category = int(val)
                            break
                        except (ValueError, TypeError):
                            pass

            # Extract Chief Complaint
            chief_complaint = "General Triage Review"
            complaint_code = "404684003"
            if cond_list:
                c0 = cond_list[0]
                chief_complaint = c0.get("code", {}).get("text") or chief_complaint
                codings = c0.get("code", {}).get("coding", [])
                if codings:
                    complaint_code = codings[0].get("code", complaint_code)

            # Extract Key Vitals
            vitals = {}
            for o in obs_list:
                cd = o.get("code", {})
                codes = [c.get("code") for c in cd.get("coding", [])]
                text = cd.get("text", "").lower()
                val = o.get("valueQuantity", {}).get("value")
                unit = o.get("valueQuantity", {}).get("unit", "")
                if val is not None:
                    if "8480-6" in codes or "systolic" in text:
                        vitals["sys_bp"] = f"{int(val)} {unit}"
                    elif "8462-4" in codes or "diastolic" in text:
                        vitals["dia_bp"] = f"{int(val)} {unit}"
                    elif "8867-4" in codes or "heart rate" in text or "pulse" in text:
                        vitals["hr"] = f"{int(val)} {unit}"
                    elif "59408-5" in codes or "spo2" in text or "oxygen" in text:
                        vitals["spo2"] = f"{int(val)}{unit}"
                    elif "8310-5" in codes or "temperature" in text:
                        vitals["temp"] = f"{val} {unit}"
                    elif "72514-3" in codes or "pain" in text:
                        vitals["pain"] = f"{int(val)}/10"

            # Arrival time
            arrival_time = "Just now"
            if enc_list and enc_list[0].get("period", {}).get("start"):
                arrival_time = enc_list[0]["period"]["start"]

            meta = MTS_META.get(mts_category, MTS_META[3])

            queue.append({
                "patient_id": pid,
                "name": display_name,
                "gender": patient.get("gender", "unknown"),
                "birthdate": patient.get("birthDate", "-"),
                "gms_number": gms_number,
                "mts_category": mts_category,
                "mts_name": meta["name"],
                "mts_target": meta["target"],
                "mts_badge": meta["badge"],
                "chief_complaint": chief_complaint,
                "complaint_code": complaint_code,
                "vitals": vitals,
                "arrival_time": arrival_time
            })

        # Sort strictly by MTS Category (1 Immediate first)
        queue.sort(key=lambda x: x["mts_category"])
        return queue
