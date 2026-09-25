"""Invoice processing pipeline for Acme Corp."""

import logging

from .ai import AIClient, AIUnavailableError, FakeAI, make_ai_client
from .database import reset_database
from .logs import LOGGER_NAME, log_event, log_to_file
from .models import AIRole, CaseFile, Finding, FindingKind, Invoice, Outcome
from .policy import ApprovalPolicy, PolicyError, load_policy, save_policy
from .pipeline import Stage, process_invoice
from .ingestion import is_supported, unsupported_format_message
from .batch import BatchProgress, InvoiceProgress, invoice_files, process_files

__all__ = [
    "AIClient",
    "AIRole",
    "AIUnavailableError",
    "ApprovalPolicy",
    "BatchProgress",
    "CaseFile",
    "FakeAI",
    "Finding",
    "FindingKind",
    "Invoice",
    "InvoiceProgress",
    "Outcome",
    "PolicyError",
    "Stage",
    "invoice_files",
    "is_supported",
    "load_policy",
    "save_policy",
    "log_event",
    "log_to_file",
    "make_ai_client",
    "process_files",
    "process_invoice",
    "reset_database",
    "unsupported_format_message",
]

# A library leaves log output to the application; see log_to_file.
logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())
