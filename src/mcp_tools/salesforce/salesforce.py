"""
Provides a class for interfacing with the Salesforce REST API. Comes with a set of
helper methods for SOQL reads plus record create/update primitives.

Object mapping (Nooks context): Account -> Nooks account, Contact -> account-linked
prospect (via Contact.AccountId), Lead -> standalone prospect (uses Lead.Company).

Makes use of the Salesforce OAuth token, found in auth.tokens.salesforce_token
"""

import os
import re
from typing import Any, Dict, List, Optional
import httpx
from dotenv import load_dotenv
from auth.tokens.salesforce_token import SalesforceToken
from mcp_tools.auth_tool_app import OAuthToolApp
from utils.decorators import tool_retry_factory, tool_scope_factory
from utils.errors import ApiRequestError, RetryableApiError

load_dotenv()
SCOPES = ["api", "refresh_token"]
API_VERSION = os.getenv('SALESFORCE_API_VERSION', 'v56.0')
ERROR_TEXT_CAP = 2000
REQUEST_TIMEOUT_SECONDS = 30
COMPOSITE_CHUNK_SIZE = 200  # Salesforce composite sObjects limit per request
MAX_BATCH_RECORDS = 1000

_IDENTIFIER_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_.]*$')
_RECORD_ID_RE = re.compile(r'^[a-zA-Z0-9]{15}$|^[a-zA-Z0-9]{18}$')


def _validate_identifier(value: str, label: str):
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        raise ApiRequestError(f"Invalid Salesforce {label}: {value!r}")


def _validate_record_id(record_id: str):
    if not isinstance(record_id, str) or not _RECORD_ID_RE.match(record_id):
        raise ApiRequestError(f"Invalid Salesforce record id: {record_id!r}")


class SalesforceToolApp(OAuthToolApp):
    def _request(
        self,
        token: SalesforceToken,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None
    ):
        url = f"{token.instance_url}{path}"
        headers = {'Authorization': f"Bearer {token.access_token}"}
        try:
            response = httpx.request(
                method, url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS
            )
        except httpx.TransportError as e:
            raise RetryableApiError(f"Salesforce network error: {e}")

        if response.status_code == 401:
            # session was killed server-side before the local TTL — force a refresh
            self.provider.invalidate_access_token(token)
            raise ApiRequestError(
                "Salesforce session expired; it will refresh automatically — retry the tool call."
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableApiError(f"Salesforce API {response.status_code}: {response.text[:ERROR_TEXT_CAP]}")
        if response.status_code >= 400:
            raise ApiRequestError(f"Salesforce API {response.status_code}: {response.text[:ERROR_TEXT_CAP]}")
        if not response.content:
            return None

        return response.json()

    @staticmethod
    def _strip_attributes(record: Dict[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in record.items() if key != 'attributes'}

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Salesforce error (soql_query)", retry_on=(RetryableApiError,))
    def soql_query(
        self, *,
        token: SalesforceToken,
        ctx: Dict[str, Any],
        soql: str,
        max_pages: int = 10
    ):
        """
        Runs arbitrary SOQL and follows nextRecordsUrl pagination up to max_pages.
        """
        result = self._request(token, 'GET', f"/services/data/{API_VERSION}/query", params={'q': soql})
        records = list(result.get('records', []))
        pages = 1
        while not result.get('done', True) and result.get('nextRecordsUrl') and pages < max_pages:
            result = self._request(token, 'GET', result['nextRecordsUrl'])
            records.extend(result.get('records', []))
            pages += 1

        return {
            'total_size': result.get('totalSize'),
            'returned': len(records),
            'truncated': not result.get('done', True),
            'records': [self._strip_attributes(record) for record in records]
        }

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Salesforce error (get_record)", retry_on=(RetryableApiError,))
    def get_record(
        self, *,
        token: SalesforceToken,
        ctx: Dict[str, Any],
        sobject: str,
        record_id: str,
        fields: Optional[List[str]] = None
    ):
        _validate_identifier(sobject, 'object name')
        _validate_record_id(record_id)
        params = None
        if fields:
            for field in fields:
                _validate_identifier(field, 'field name')
            params = {'fields': ','.join(fields)}

        record = self._request(
            token, 'GET',
            f"/services/data/{API_VERSION}/sobjects/{sobject}/{record_id}",
            params=params
        )
        return self._strip_attributes(record)

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Salesforce error (create_record)", retry_on=(RetryableApiError,))
    def create_record(
        self, *,
        token: SalesforceToken,
        ctx: Dict[str, Any],
        sobject: str,
        fields: Dict[str, Any]
    ):
        _validate_identifier(sobject, 'object name')
        for field in fields:
            _validate_identifier(field, 'field name')

        result = self._request(
            token, 'POST',
            f"/services/data/{API_VERSION}/sobjects/{sobject}",
            json_body=fields
        )
        if not result or not result.get('success', False):
            raise ApiRequestError(f"Salesforce create failed: {str(result)[:ERROR_TEXT_CAP]}")

        return {
            'id': result['id'],
            'sobject': sobject,
            'url': f"{token.instance_url}/{result['id']}"
        }

    @tool_scope_factory(scopes=SCOPES)
    def create_records(
        self, *,
        token: SalesforceToken,
        ctx: Dict[str, Any],
        records: List[Dict[str, Any]],
        all_or_none: bool = False
    ):
        """
        Bulk create via the composite sObjects API, chunked at 200 records per request.
        No whole-method retry — a retry after a partially applied batch would duplicate
        records; failed chunks are reported per record index instead.
        """
        if not records:
            raise ApiRequestError("records list is empty")
        if len(records) > MAX_BATCH_RECORDS:
            raise ApiRequestError(
                f"batch too large: {len(records)} records > {MAX_BATCH_RECORDS}; split across multiple calls"
            )

        payloads = []
        for record in records:
            sobject = record.get('sobject')
            fields = record.get('fields') or {}
            _validate_identifier(sobject, 'object name')
            for field in fields:
                _validate_identifier(field, 'field name')
            payloads.append({'attributes': {'type': sobject}, **fields})

        created, failed = [], []
        for start in range(0, len(payloads), COMPOSITE_CHUNK_SIZE):
            chunk = payloads[start:start + COMPOSITE_CHUNK_SIZE]
            try:
                results = self._request(
                    token, 'POST',
                    f"/services/data/{API_VERSION}/composite/sobjects",
                    json_body={'allOrNone': all_or_none, 'records': chunk}
                )
            except (ApiRequestError, RetryableApiError) as e:
                failed.extend(
                    {'index': start + offset, 'errors': [str(e)[:500]]}
                    for offset in range(len(chunk))
                )
                continue

            for offset, result in enumerate(results):
                index = start + offset
                if result.get('success'):
                    created.append({
                        'index': index,
                        'id': result['id'],
                        'sobject': records[index]['sobject']
                    })
                else:
                    failed.append({'index': index, 'errors': result.get('errors', [])})

        return {
            'requested': len(records),
            'created_count': len(created),
            'failed_count': len(failed),
            'created': created,
            'failed': failed
        }

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Salesforce error (update_record)", retry_on=(RetryableApiError,))
    def update_record(
        self, *,
        token: SalesforceToken,
        ctx: Dict[str, Any],
        sobject: str,
        record_id: str,
        fields: Dict[str, Any]
    ):
        _validate_identifier(sobject, 'object name')
        _validate_record_id(record_id)
        for field in fields:
            _validate_identifier(field, 'field name')

        self._request(
            token, 'PATCH',
            f"/services/data/{API_VERSION}/sobjects/{sobject}/{record_id}",
            json_body=fields
        )
        return {
            'id': record_id,
            'sobject': sobject,
            'updated_fields': sorted(fields)
        }


def main():
    pass
