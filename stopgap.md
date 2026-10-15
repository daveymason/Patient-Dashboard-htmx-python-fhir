# Slainte-Engine: Architecture Audit, Stopgap Assessment & Unification Plan

**Document:** `stopgap.md`  
**Date:** October 13, 2026  
**Status:** In Progress — Steps 1 & 2 Completed  

---

## 1. Executive Summary & Inventory

The project currently consists of three distinct components that were developed independently and are now being unified into the **Slainte-Engine Clinical Interoperability Appliance**:

| Component | Working Name | Host Location | Appliance Role |
| :--- | :--- | :--- | :--- |
| **Ingestion Agent** | `SlainteEngine` | `./openhands/` | Autonomous worker loop that scans `/data/raw/`, resolves clinical schemas, enforces terminology, and outputs valid FHIR R4 bundles. |
| **Deterministic Tool Server** | `SlainteScan` | `./slainte-scan-mcp/` & `../OCR2FHIR/` | Model Context Protocol (MCP) server providing zero-hallucination tools: MTS category calculation, SNOMED-CT / LOINC mapping, and FHIR schema validation. |
| **Clinical UI** | `SlainteDash` | `./frontend/` & `../Patient-Dashboard-htmx-python-fhir/` | High-performance HTMX + Flask dashboard providing the Live Triage Queue, Pipeline Telemetry, and Patient Clinical Detail views. |

---

## 2. Monorepo vs. Independent Repositories: Architectural Justification

You asked: *"The other two repos are already live. Tell me why this structure is better. Justify why this is good."*

Here is the direct, balanced breakdown comparing the **Unified Monorepo** against the **Independent Multi-Repo (Polyrepo)**:

### Option A: The Unified Monorepo (Why It Is Better for an Appliance)
* **1. Zero Contract Drift & Atomic Commits:**
  Clinical data contracts are strict and unforgiving. When we added `labs`, `medications`, and `allergies` to the pipeline, we had to update:
  * The schema scanner (`slainte-scan-mcp`)
  * The FHIR bundle compiler and validator (`slainte-scan-mcp`)
  * The agent ingestion parser loop (`openhands`)
  * The dashboard query engine & templates (`frontend`)
  In a **monorepo**, this entire clinical pipeline upgrade exists in **ONE single git commit**. In a multi-repo setup, you have to submit 3 separate pull requests across 3 repositories, coordinate version tags, and risk deployment lockup (e.g. if a user pulls Dashboard v1.4 with MCP v1.1, the ingestion crashes).
* **2. Turnkey Single-Command Deployment (`docker compose up --build`):**
  A hospital IT engineer or field clinic operator clones **one** repository and runs one command. Docker Compose builds from local directories without git submodules failing, private repo authentication issues, or missing relative paths.
* **3. Unified End-to-End Pipeline CI/CD:**
  A single test script (`scripts/test_pipeline.sh`) spins up the MCP, runs the ingestion loop, validates the bundles, and asserts the UI output in one CI job.

### Option B: Keeping the Repos Independent (Polyrepo)
* **When this makes sense:** If you want `OCR2FHIR` (SlainteScan) to be an independent open-source library that external developers install via `pip install slainte-scan` or plug into Claude Desktop as a generic MCP server without knowing or caring about Slainte-Engine.
* **The Cost:** You must maintain separate versioning, synchronize shared models manually or via published packages, and handle cross-repo issues.

### The Recommended Hybrid Compromise:
Keep `slainte-engine` as the **unified monorepo source of truth** for development, automated testing, and Docker appliance deployment. If you want the other two GitHub repos to stay live and updated for public stars/visibility, use a simple GitHub Action or `git subtree push` to sync the `frontend` and `slainte-scan-mcp` folders to their respective standalone repositories automatically on release!

---

## 3. Immediate Stopgap Roadmap & Status

### [COMPLETED] Step 1: Fix Sidebar URL History & Direct View Routing
* **Implemented:**
  * Added `hx-push-url="/?view={{ key }}"` to all navigation links in `sidebar.html`.
  * Updated `htmx-setup.js` with active navigation synchronization (`syncActiveNav`) on `DOMContentLoaded`, `popstate`, and `htmx:afterSwap`.
  * Configured `app.py` so direct browser navigation to `/fhir/Triage`, `/pipeline/status`, `/fhir/Patients`, `/fhir/Demographics`, and `/fhir/Dashboard` cleanly redirects to `/?view=...` to boot the full styled shell.
* **Result:** The address bar immediately updates when clicking tabs, browser Back and Forward buttons work as expected, and bookmarking/reloading preserves the active view.

### [COMPLETED] Step 2: Collapsible MTS Triage Queue Interface
* **Implemented:**
  * Replaced the flat triage table with 5 collapsible priority accordions (`<details class="mts-category-card">`) matching official Manchester Triage System standards:
    * 🔴 **Cat 1: Immediate (0 min)** — Expanded by default
    * 🟠 **Cat 2: Very Urgent (10 min)** — Expanded by default
    * 🟡 **Cat 3: Urgent (60 min)** — Expanded by default
    * 🟢 **Cat 4: Standard (120 min)** — Collapsed by default
    * 🔵 **Cat 5: Non-Urgent (240 min)** — Collapsed by default
  * Added action toolbar buttons:
    * `Expand All`
    * `Focus Urgent (Collapse Cat 4-5)`
    * `Collapse All`
  * Added session persistence in `htmx-setup.js` so clinician expand/collapse preferences are remembered across 5-second live HTMX queue refreshes.
  * Added smooth chevron rotation animations in `styles.css`.

### [COMPLETED] Step 3: Add Patient Intake & File Upload UI
* **Implemented:**
  * Top-bar **"Patient Intake / Upload"** button triggering an interactive Bootstrap modal (`intake_modal.html`).
  * **Tab 1: Fast Triage Intake Form**: Clinician enters patient name, Irish GMS number, chief complaint, and physiological vitals (BP, HR, SpO2, RR, Temp, Pain).
    * Submits via HTMX to `/fhir/intake`.
    * Computes Manchester Triage category deterministically via `clinical_intake.py`.
    * Constructs and validates an HL7 FHIR R4 Bundle with LOINC vitals and SNOMED-CT clinical codes.
    * Persists directly to `/workspace/data/fhir_store` for sub-second UI updates while non-blockingly logging to `/workspace/data/raw/manual_intake_patients.csv` for OpenHands agent synchronization.
    * Renders a rich clinical confirmation banner with category badge, wait time target, and direct links to the patient card and queue.
  * **Tab 2: Batch Clinical File Upload**: Drag & drop CSV/TSV/JSON upload dropzone.
    * Submits multipart form via HTMX to `/fhir/upload`.
    * Safely stages the file to `/workspace/data/raw/` where the OpenHands background daemon automatically ingests, validates, and incorporates records into the live queue.

### [COMPLETED] Step 4: Scale Test Patient Roster
* **Implemented:**
  * Expanded emergency triage cohort from 5 to 18 diverse emergency records (`emergency_triage_batch_01.csv`), spanning all 5 MTS categories:
    * **Cat 1 (Immediate - 0m)**: Mary Walsh (MI / Cardiogenic shock), David Brennan (Severe anaphylaxis & stridor), Ciara O Connor (Massive hematemesis & hemorrhagic shock), Aoife Gallagher (Febrile infant with petechial rash).
    * **Cat 2 (Very Urgent - 10m)**: Patrick Byrne (Severe acute asthma / dyspnea), Declan Foley (Excruciating renal colic 8/10).
    * **Cat 3 (Urgent - 60m)**: Siobhan Kelly (Acute abdomen), Sean McCarthy (Acute stroke), Michael Connolly (Paracetamol overdose), Conor O Brien (Compound open fracture), Roisin Dunphy (Boiling water scalds 8% TBSA), Niamh Quigley (Colles fracture).
    * **Cat 4 (Standard - 120m)**: Liam Murphy (Ankle sprain), Brian Maguire (Scalp laceration), Eimear Hayes (Cystitis/UTI), Dermot Keane (Suture removal).
    * **Cat 5 (Non-Urgent - 240m)**: Fiona O Reilly (Chronic back ache review), Sinead Ryan (Viral cold / reassurance).
  * Generated 18 verified FHIR R4 bundles in `/data/fhir_store/` with full LOINC, SNOMED-CT, and Irish GMS OID compliance.

### [COMPLETED] Step 5: Clean Monorepo Consolidation & Unified Executive CLI
* **Implemented:**
  * Consolidated `frontend/` (SlainteDash), `slainte-scan-mcp/` (SlainteScan), and `openhands/` (SlainteEngine) under `slainte-engine`.
  * Created turnkey executive CLI script [`run.sh`](file:///home/adman-voids/Desktop/Code/slainte-engine/run.sh) and root [`Makefile`](file:///home/adman-voids/Desktop/Code/slainte-engine/Makefile).
  * Automated environment creation from `.env.example`, persistent storage volume permissions, container health probing, and access telemetry.
