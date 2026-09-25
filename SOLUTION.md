# Invoice Processing: the Solution

A working multi-agent system that takes Acme Corp's vendor Invoices from a file to a decision. Each Invoice is read, screened for fraud, checked against what Acme actually received, approved or stopped, and paid only when every check passes. Every Invoice ends as **Approved** (paid), **Rejected** (never paid) or **Held** (waiting for a person), and gets a Case File that explains why in plain English.

It runs locally, uses LangGraph to orchestrate the agents, and uses Grok as its only outside service.

## Quick start

You need Python 3.11 or newer and an xAI API key.

```bash
python3 -m venv .venv                   # Windows: python -m venv .venv
source .venv/bin/activate               # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
cp .env.example .env                    # Windows: copy .env.example .env   then put your key after XAI_API_KEY=
python main.py --invoice_path=data/invoices/invoice_1001.txt # To run an individual invoice
```

You can skip the `.env` step. If no key is set, the command asks you to paste one and offers to save it. You can also pass it with `--api_key=...`.

**What happens:**

- The terminal shows each Invoice moving through the stages, then a summary table.
- The results page opens in your browser.
- The last line in the terminal gives the page's address and says to press Ctrl+C to stop.

The 20 sample Invoices take about 9 minutes with Grok. You can run all the invoices in one go too: `python main.py --invoice_path=data/invoices`.

| Option           | What it does                                                       |
| ---------------- | ------------------------------------------------------------------ |
| `--invoice_path` | An Invoice file, or a folder of them (processed in filename order) |
| `--api_key`      | The xAI key, if it isn't in the environment or `.env`              |
| `--no-ui`        | Don't open the results page                                        |
| `--reset`        | Wipe the processing history and restore the starting Stock first   |

The inventory database is created on first run from the brief's seed data. Processing history is kept between runs, so a second run of the same folder Holds already-paid Invoices as Duplicates. Use `--reset` to start again from a clean slate.

## What it does

```
Invoice file ─▶ Ingestion ─▶ Fraud Screen ─▶ Validation ─▶ Approval ─┬─▶ Rejected or Held (reasons recorded)
                                                                     ├─▶ Approved ─▶ Payment
                                                                     └─▶ over $10,000 ─▶ VP Review ⇄ Critique
                                                                                         ─▶ Approved ─▶ Payment
                                                                                         ─▶ Held
```

| Stage            | Done by                                                    | What happens                                                                                                                          |
| ---------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| **Ingestion**    | Code for JSON, CSV and XML; the AI Reader for text and PDF | Every format becomes one standard Invoice. PDF text is extracted by a PDF library first, so the AI only ever reads text.              |
| **Fraud Screen** | AI                                                         | Reads the whole original document for signs it isn't genuine, and quotes the evidence for each one.                                   |
| **Validation**   | Code only                                                  | Checks the Invoice against the inventory database and itself (the full list is below).                                                |
| **Approval**     | Code, then AI for large Invoices                           | The Approval Policy decides. An Invoice with no problems over $10,000 goes to an AI VP Review, whose reasoning a second AI critiques. |
| **Payment**      | Code                                                       | Calls the brief's mock payment, only for Approved Invoices.                                                                           |

**The four AI agents**, all on Grok:

- **Reader:** turns messy text into the standard Invoice. Any value it had to infer, such as a garbled date, is declared as a _Guessed Field_, saying what the document showed and what it chose.
- **Fraud Screen:** looks for pressure to pay, unusual payment requests, and implausible details like a fictional address. Each signal must quote the document.
- **VP Review:** a tool-using agent. It looks up Stock, the Vendor's past Invoices and the Approval Policy, then decides Approved or Held and explains why in a few sentences.
- **Critique:** checks the VP's reasoning for loose ends, unsupported claims and conflicts with the policy.

**Self-correction:**

- Every answer is checked against a typed schema.
- The Reader's answer must also add up: Line Items, subtotal, tax, shipping and total must agree to the cent. A wrong answer goes back to the AI with the specific problems, up to 2 retries.
- If the Critique objects, the VP revises, up to 2 times. If they still disagree, the Invoice is Held.

**What Validation checks:**

- **Stock:** quantities of the same item are added up across all lines before checking Stock, so splitting an order can't hide a **Shortfall**.
- **Item names:** matched while ignoring spaces, capitals and dashes. Any name that still doesn't match is an **Unknown Item**.
- **Data:** zero or negative quantities; arithmetic to the cent; a missing Vendor, total or due date; and non-USD currency.
- **Duplicates:** an Invoice whose number and Vendor match one already Approved, including a revised copy, is a **Duplicate**.

## Outcomes and the Approval Policy

Each problem found is a _Finding_. The Approval Policy in [config/approval_policy.yaml](config/approval_policy.yaml) says what each kind of Finding leads to:

| Rejected (never paid)                             | Held (a person decides)                                       |
| ------------------------------------------------- | ------------------------------------------------------------- |
| Shortfall: bills more units than we have in Stock | Fraud Signal: the AI saw signs the Invoice may not be genuine |
| Unknown Item                                      | Guessed Field: the AI had to infer a value                    |
| Zero or negative quantity                         | Unreadable document                                           |
| Totals that don't add up                          | The AI couldn't read or screen it (e.g. Grok unreachable)     |
| No Vendor                                         | Duplicate of an Approved Invoice                              |
| No total                                          | Missing or unreadable due date                                |
|                                                   | Not in USD                                                    |

Four rules decide the Outcome:

- The most severe Outcome across all Findings wins: Rejected over Held.
- An Invoice with no Findings at or under the review threshold ($10,000) is Approved and paid.
- An Invoice with no Findings over the threshold goes to VP Review, which can Approve or Hold it but never Reject it.
- Rejected and Held Invoices are logged with their reasons and never paid.

**To change the policy**, use the editor on the results page, or edit the YAML file and run again. For example, change `shortfall: Rejected` to `shortfall: Held`, or change `review_threshold: 10000`. The page and the command line use the same file and the same checks: a typo stops the run with a clear message, and an invalid edit on the page is refused and not saved. It never silently approves anything.

## Seeing the results

**The results page** (Streamlit, on your machine only):

- **This run:** the Invoices you just processed, each with a coloured Outcome and its main reason.
- **History:** every Invoice processed so far. Copies of the same Invoice sit together, so Duplicates are side by side.
- Select any Invoice to read its full **Case File**: the data read from it, every Finding, Guessed Fields, Fraud Signals with their quotes, each VP Review and Critique round (with the tools the VP called), and the payment result.
- The **Process panel** in the sidebar processes a sample Invoice or a file you upload, showing each stage as it runs. On an empty history, one button processes all the samples.
- The **Approval Policy editor**, below it, changes the review threshold and whether each kind of Finding is Rejected or Held. It saves to the policy file, keeping the file's comments.
- Select one or more Invoices and press **Process again** to see how the current policy decides them. The new result sits next to the old one in History. An Invoice that was already Approved comes back as a Duplicate, because processing it again would mean paying it twice.

**On disk:**

- `output/` has one Case File per processed Invoice (JSON, never overwritten).
- `output/pipeline.log` has structured logs: one JSON line per stage, AI call, retry and result, with timings.

## Design decisions

**The AI can only make an Outcome stricter.** The rules run first. If the Approval Policy Rejects an Invoice, no AI ever sees it for approval. The VP Review only sees Invoices the rules found no problems with, and its answer can only be Approved or Held. An AI mistake can never cause a payment the rules had already blocked. The short version for a finance audience: rules protect the money, AI handles judgement calls.

**Only checked facts Reject; AI doubt goes to a person.** An Invoice is Rejected only for something our own code verified: a Shortfall, an Unknown Item, totals that don't add up. Anything that rests on the AI is Held for a person: a Fraud Signal, a Guessed Field, a document it couldn't read, or an Invoice it couldn't screen at all. An AI hunch isn't evidence we'd defend to a Vendor. The brief's INV-1004 shows why: its Vendor address is a cartoon character's house, but its goods are in Stock and its numbers add up, so it deserves a look, not a refusal. For the same reason the system fails closed: if Grok can't be reached, an Invoice isn't paid unscreened. It's Held for a person.

**Code reads what's reliable; the AI reads what's messy.** JSON, CSV and XML are parsed by code and never touched by AI mistakes. Text and PDF go to the AI Reader, and code checks its work before anything else happens. The Reader must declare every value it inferred, and any Guessed Field is Held for now, so it's clear where the AI filled gaps.

**A document that is wrong is judged, not "unreadable".** Sometimes the Reader's totals still don't add up after its retries. If every number in its reading appears in the document, and each line adds up on its own, the document itself is wrong. The Invoice then goes on to Validation, so a PDF gets the same Outcome as the same Invoice sent as JSON. Otherwise it's treated as a possible misreading and Held.

**A Duplicate means paying twice.** Only a match with an Invoice that was already Approved counts. A Vendor can resubmit a corrected copy of a Rejected or Held Invoice, and it's processed normally.

**One pipeline, two front ends.** The command line and the results page call the same entry point and share the same processing loop. Neither contains any decision logic.

## Business impact

Against the three problems in the brief:

- **5-day processing delays → minutes.**
  - In the last full run with Grok, a structured Invoice took about 13 seconds end to end. A text or PDF Invoice the AI had to read took about 43 seconds on average, and never more than 67.
  - The VP approval that used to mean an email chain now happens in the same run, for the Invoices over $10,000 that need it.
  - Held Invoices still wait for a person, but they arrive with the reason attached instead of needing to be investigated from scratch.
- **30% error rate → errors stopped at the door.**
  - Every kind of problem in the sample set is caught before payment: over-billing, unknown items, negative quantities, bad arithmetic, missing Vendor or due date, foreign currency, a second copy of a paid Invoice, and fraud tactics.
  - Nothing is paid unless every check passes, and nobody re-types anything.
  - When the AI had to guess a value, the Invoice is flagged, not quietly "fixed".
- **$2M a year in manual handling → people only handle exceptions.**
  - Clean Invoices at or under the threshold go from file to payment with no one touching them.
  - Staff time moves to the Held Invoices, and each arrives with its reason and evidence.
  - Acme reports a 30% error rate. If that holds, roughly 70% of Invoices could flow straight through. That estimate rests on the error rate alone: we don't have Acme's cost breakdown.
- **Also:**
  - Every decision is auditable. The Case File shows what was read, what was found and why, including each AI step and how long it took.
  - The rules live in one file a finance lead can change without an engineer, on the results page or in the file itself.

## Project layout

| Path                                                               | What's there                                                          |
| ------------------------------------------------------------------ | --------------------------------------------------------------------- |
| `main.py`                                                          | The command line                                                      |
| `results_page.py`                                                  | The Streamlit results page                                            |
| `invoice_pipeline/pipeline.py`                                     | The LangGraph graph and the single entry point, `process_invoice`     |
| `invoice_pipeline/batch.py`                                        | The processing loop the command line and the page share               |
| `invoice_pipeline/readers.py`, `ingestion.py`                      | Reading JSON, CSV and XML by code; routing text and PDF to the Reader |
| `invoice_pipeline/ai_reader.py`, `fraud_screen.py`, `vp_review.py` | The four AI agents                                                    |
| `invoice_pipeline/ai.py`                                           | The Grok client and the shared ask-check-retry loop                   |
| `invoice_pipeline/validation.py`, `policy.py`                      | The checks, and applying the Approval Policy                          |
| `invoice_pipeline/database.py`, `payment.py`, `case_files.py`      | Stock and processing history, the mock payment, Case Files            |
| `config/approval_policy.yaml`                                      | The Approval Policy                                                   |
| `tests/`                                                           | The test suite                                                        |
