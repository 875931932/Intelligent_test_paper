# AI 试卷质量验证原型 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a local, non-production prototype that validates whether `deepseek-v4-flash` can create evidence-grounded university final-exam papers from teacher-uploaded course materials.

**Architecture:** A Python standard-library HTTP server keeps one course's working state in memory. It stores uploaded source files only in a local prototype cache, extracts text from supported formats after an explicit teacher action, turns that text into candidate evidence and knowledge points, publishes the candidate index only after confirmation, then uses a basic form to create a deterministic per-question blueprint and sends only each question's retrieved evidence package to the model.

**Tech Stack:** Python 3.12+, `pypdf`, `python-docx`, `python-pptx`, static HTML/CSS/JavaScript, OpenAI-compatible DeepSeek Chat Completions API.

---

### Task 1: Make the prototype runnable without hidden setup

**Files:**

- Create: `prototype/.env.example`
- Create: `prototype/requirements.txt`
- Create: `prototype/README.md`

- [ ] **Step 1: Add the exact environment-variable contract**

Create `prototype/.env.example` with the following non-secret defaults:

```dotenv
# Copy this file to .env and set only the API key locally. Never commit .env.
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-v4-flash
# Set true only when the deployed endpoint supports response_format=json_object.
DEEPSEEK_JSON_MODE=false
PROTOTYPE_HOST=127.0.0.1
PROTOTYPE_PORT=8787
```

- [ ] **Step 2: Declare the parser-only dependencies**

Create `prototype/requirements.txt` with exactly:

```text
pypdf>=5.0,<7
python-docx>=1.1,<2
python-pptx>=1.0,<2
```

- [ ] **Step 3: Document the teacher test loop and the safety boundary**

Create `prototype/README.md` explaining: install dependencies; copy `.env.example` to `.env`; start `python server.py`; open `http://127.0.0.1:8787`; upload → organize → inspect candidate knowledge points → publish → build blueprint → generate; and that source excerpts are sent to the configured model only during organization/generation. State that this is not a multi-course, persistent, LangGraph, database, OCR, export, or production-security implementation.

- [ ] **Step 4: Verify configuration artifacts are usable**

Run:

```powershell
Get-Content prototype\.env.example
Get-Content prototype\requirements.txt
```

Expected: the default model is `deepseek-v4-flash` and all three parser packages are listed.

### Task 2: Align reset and file-type behavior with the local-only claim

**Files:**

- Modify: `prototype/server.py:21-40, 120-142, 1140-1165, 1171-1219`
- Modify: `prototype/static/index.html:39-49, 111`

- [ ] **Step 1: Write the reset behavior to protect repeatable quality tests**

Add a helper that removes only files inside the resolved `prototype/.prototype-data/uploads` directory, recreates it, and never resolves a deletion target outside `prototype/.prototype-data`. Call it from `POST /api/reset` before `reset_state()`.

- [ ] **Step 2: Make accepted upload types match extractors**

Use one server-side extension set for `.pdf`, `.docx`, `.pptx`, `.txt`, and `.md`; remove `.doc` from the upload input and server allow-list because the prototype does not extract old binary Word files. The server must keep giving a clear error if a manually submitted unsupported suffix is received.

- [ ] **Step 3: Update the visible local-storage wording**

Change the footer and reset confirmation to state that reset clears the current in-memory state **and the locally staged prototype uploads**, while service restart clears only memory and leaves the temporary upload cache until reset or manual deletion.

- [ ] **Step 4: Verify the implementation has no broad deletion target**

Run:

```powershell
& 'C:\Users\Gnahz\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m py_compile prototype\server.py
```

Expected: exit code `0`.

### Task 3: Exercise the no-key end-to-end workflow

**Files:**

- Create: `prototype\fixtures\course-outline.txt`
- Create: `prototype\smoke_test.ps1`

- [ ] **Step 1: Add a small self-contained course fixture**

Create a Chinese text fixture containing three short sections about supervised fine-tuning, LoRA parameters, and RAG evaluation. Each section must state at least one directly testable fact, so the fallback knowledge-point extraction and evidence retrieval have usable source text.

- [ ] **Step 2: Add a PowerShell smoke test**

Create `prototype\smoke_test.ps1` that starts `server.py` with the bundled Python on a temporary local port, waits for `/api/health`, uploads `fixtures/course-outline.txt` with `Invoke-WebRequest -Form`, calls `/api/organize`, asserts candidate chunks and knowledge points exist, calls `/api/publish`, builds a 10-point blueprint consisting of two 2-point choice questions and two 3-point short-answer questions, asserts that the index version is `1` and four plan items exist, calls `/api/reset`, asserts the fixture upload is no longer in state, then always stops the server process in a `finally` block.

- [ ] **Step 3: Run the smoke test**

Run:

```powershell
pwsh -ExecutionPolicy Bypass -File prototype\smoke_test.ps1
```

Expected: a terminal line ending in `SMOKE TEST PASSED`.

### Task 4: Verify the model-facing path and hand off the live quality evaluation

**Files:**

- Modify: `prototype/README.md`
- Verify: `prototype/server.py`, `prototype/static/app.js`

- [ ] **Step 1: Check static code syntax**

Run:

```powershell
& 'C:\Users\Gnahz\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' --check prototype\static\app.js
```

Expected: exit code `0`.

- [ ] **Step 2: Add an explicit live-model test checklist**

In `README.md`, add the two supplied course-outline PDF paths as suggested test inputs and ask the teacher to assess each generated question for scope evidence, answer correctness, ambiguity, difficulty, scoring closure, and duplication. State that no live model call is made unless `DEEPSEEK_API_KEY` is present.

- [ ] **Step 3: Run the local validation commands**

Run the Python compilation, JavaScript syntax check, and smoke test. Do not attempt a real external model call without a configured local key.
