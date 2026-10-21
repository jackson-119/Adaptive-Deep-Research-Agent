"""Retry invalid output and transient failures, never budgets or authentication."""
import hashlib
import json

EVALUATION_VERSION = 'final_report_retry_v2'


def retryable(exc):
    return isinstance(exc, (ValueError, KeyError, TimeoutError, ConnectionError)) or type(exc).__name__ in {
        'APITimeoutError', 'APIConnectionError', 'RateLimitError'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def evaluation_identity(state):
    return {'version': EVALUATION_VERSION,
            'input_sha256': digest({k: state.get(k) for k in
                                   ('report', 'plan', 'evidence', 'raw_documents')}),
            'report_sha256': digest(state['report'])}
