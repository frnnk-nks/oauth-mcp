"""
Provides a class for interfacing with the Outreach JSON:API. Comes with helper methods
for prospect/account CRUD, sequence and sequence-step creation, sequence enrollment,
and one-off task creation.

Object mapping (Nooks context): Outreach account -> Nooks account, Outreach prospect ->
Nooks prospect, linked to an account through the JSON:API relationships.account
resource identifier.

Makes use of the Outreach OAuth token, found in auth.tokens.outreach_token
"""

import os
import time
from typing import Any, Dict, List, Optional
import httpx
from dotenv import load_dotenv
from auth.tokens.outreach_token import OutreachToken
from mcp_tools.auth_tool_app import OAuthToolApp
from utils.decorators import tool_retry_factory, tool_scope_factory
from utils.errors import ApiRequestError, RetryableApiError

load_dotenv()
SCOPES = [
    "accounts.all",
    "prospects.all",
    "sequences.all",
    "sequenceSteps.all",
    "sequenceStates.all",
    "sequenceTemplates.all",
    "tasks.all",
    "templates.all",
    "mailboxes.read",
    "users.read"
]
API_BASE_URL = os.getenv('OUTREACH_API_BASE_URL', 'https://api.outreach.io/api/v2')
JSON_API_CONTENT_TYPE = 'application/vnd.api+json'
ERROR_TEXT_CAP = 2000
REQUEST_TIMEOUT_SECONDS = 30
MAX_PAGE_LIMIT = 100
MAX_BATCH_ITEMS = 100
BATCH_PACING_SECONDS = 0.15

SHARE_TYPES = {'private', 'read_only', 'shared'}
STEP_TYPES = {'auto_email', 'manual_email', 'call', 'task'}

ACCOUNT_SUMMARY = ['name', 'domain', 'websiteUrl', 'createdAt']
PROSPECT_SUMMARY = ['firstName', 'lastName', 'emails', 'title', 'company', 'tags', 'createdAt', 'updatedAt']
SEQUENCE_SUMMARY = ['name', 'description', 'shareType', 'enabled', 'sequenceStepCount', 'createdAt']
SEQUENCE_STEP_SUMMARY = ['stepType', 'order', 'interval', 'taskNote', 'createdAt']
TEMPLATE_SUMMARY = ['name', 'subject', 'shareType', 'trackLinks', 'trackOpens', 'createdAt']
SEQUENCE_TEMPLATE_SUMMARY = ['isReply', 'enabled', 'createdAt']
SEQUENCE_STATE_SUMMARY = ['state', 'createdAt']
TASK_SUMMARY = ['action', 'note', 'dueAt', 'state', 'createdAt']
MAILBOX_SUMMARY = ['email', 'sendDisabled', 'syncActiveState']
USER_SUMMARY = ['email', 'firstName', 'lastName', 'locked']


def _normalize_id(value: Any, label: str) -> int:
    """
    Outreach write payloads reject string IDs, so coerce to a positive integer.
    """
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        raise ApiRequestError(f"Invalid Outreach {label}: {value!r}")
    if normalized <= 0:
        raise ApiRequestError(f"Invalid Outreach {label}: {value!r}")
    return normalized


def _relationship(resource_type: str, resource_id: Any) -> Dict[str, Any]:
    return {'data': {'type': resource_type, 'id': _normalize_id(resource_id, f"{resource_type} id")}}


def _compact(attributes: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in attributes.items() if value is not None}


def _validate_batch(items: List[Any], label: str):
    if not items:
        raise ApiRequestError(f"{label} list is empty")
    if len(items) > MAX_BATCH_ITEMS:
        raise ApiRequestError(
            f"batch too large: {len(items)} {label} > {MAX_BATCH_ITEMS}; split across multiple calls"
        )


def _prospect_payload(
    emails: List[str],
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    title: Optional[str] = None,
    company: Optional[str] = None,
    tags: Optional[List[str]] = None,
    account_id: Optional[str] = None,
    owner_id: Optional[str] = None,
    extra_attributes: Optional[Dict[str, Any]] = None
):
    if not emails:
        raise ApiRequestError("emails is required for a prospect")
    attributes = {
        'emails': emails,
        'firstName': first_name,
        'lastName': last_name,
        'title': title,
        'company': company,
        'tags': tags,
        **(extra_attributes or {})
    }
    relationships = {}
    if account_id is not None:
        relationships['account'] = _relationship('account', account_id)
    if owner_id is not None:
        relationships['owner'] = _relationship('user', owner_id)
    return attributes, relationships


def _task_payload(
    prospect_id: str,
    action: str = 'action_item',
    note: Optional[str] = None,
    due_at: Optional[str] = None,
    owner_id: Optional[str] = None
):
    attributes = {'action': action, 'note': note, 'dueAt': due_at}
    # tasks link to prospects via the polymorphic 'subject' relationship;
    # writing 'prospect' directly is rejected as a private relationship
    relationships = {'subject': _relationship('prospect', prospect_id)}
    if owner_id is not None:
        relationships['owner'] = _relationship('user', owner_id)
    return attributes, relationships


class OutreachToolApp(OAuthToolApp):
    def _request(
        self,
        token: OutreachToken,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None
    ):
        url = f"{API_BASE_URL}{path}"
        headers = {
            'Authorization': f"Bearer {token.access_token}",
            'Accept': JSON_API_CONTENT_TYPE
        }
        if json_body is not None:
            headers['Content-Type'] = JSON_API_CONTENT_TYPE
        try:
            response = httpx.request(
                method, url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS
            )
        except httpx.TransportError as e:
            raise RetryableApiError(f"Outreach network error: {e}")

        if response.status_code == 401:
            # token died before local expiry — force a refresh on the next call
            self.provider.invalidate_access_token(token)
            raise ApiRequestError(
                "Outreach token expired; it will refresh automatically — retry the tool call."
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableApiError(f"Outreach API {response.status_code}: {response.text[:ERROR_TEXT_CAP]}")
        if response.status_code >= 400:
            raise ApiRequestError(f"Outreach API {response.status_code}: {response.text[:ERROR_TEXT_CAP]}")
        if not response.content:
            return None

        return response.json()

    @staticmethod
    def _slim(record: Dict[str, Any], attribute_names: List[str]) -> Dict[str, Any]:
        """
        Reduce a JSON:API record to its id plus a summary attribute set, so tool output
        stays readable (full Outreach records carry 100+ attributes).
        """
        attributes = record.get('attributes', {})
        slim = {'id': record.get('id'), 'type': record.get('type')}
        for name in attribute_names:
            if name in attributes:
                slim[name] = attributes[name]

        relationship_ids = {}
        for name, rel in (record.get('relationships') or {}).items():
            data = rel.get('data') if isinstance(rel, dict) else None
            if isinstance(data, dict):
                relationship_ids[name] = data.get('id')
        if relationship_ids:
            slim['relationships'] = relationship_ids

        return slim

    def _list(
        self,
        token: OutreachToken,
        path: str,
        filters: Dict[str, Any],
        summary_fields: List[str],
        limit: int
    ):
        params = {key: value for key, value in filters.items() if value is not None}
        params['page[limit]'] = max(1, min(limit, MAX_PAGE_LIMIT))
        result = self._request(token, 'GET', path, params=params)
        records = [self._slim(record, summary_fields) for record in result.get('data', [])]

        return {
            'count': len(records),
            'has_more': bool((result.get('links') or {}).get('next')),
            'records': records
        }

    def _create(
        self,
        token: OutreachToken,
        path: str,
        resource_type: str,
        attributes: Dict[str, Any],
        relationships: Optional[Dict[str, Any]] = None,
        summary_fields: Optional[List[str]] = None
    ):
        data: Dict[str, Any] = {'type': resource_type, 'attributes': _compact(attributes)}
        if relationships:
            data['relationships'] = relationships

        result = self._request(token, 'POST', path, json_body={'data': data})
        return self._slim(result['data'], summary_fields or [])

    # --- prospects ---

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Outreach error (find_prospects)", retry_on=(RetryableApiError,))
    def find_prospects(
        self, *,
        token: OutreachToken,
        ctx: Dict[str, Any],
        emails: Optional[List[str]] = None,
        prospect_ids: Optional[List[str]] = None,
        account_id: Optional[str] = None,
        limit: int = 50
    ):
        filters = {}
        if emails:
            filters['filter[emails]'] = ','.join(emails)
        if prospect_ids:
            filters['filter[id]'] = ','.join(str(_normalize_id(i, 'prospect id')) for i in prospect_ids)
        if account_id:
            filters['filter[account][id]'] = _normalize_id(account_id, 'account id')

        return self._list(token, '/prospects', filters, PROSPECT_SUMMARY, limit)

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Outreach error (get_prospect)", retry_on=(RetryableApiError,))
    def get_prospect(self, *, token: OutreachToken, ctx: Dict[str, Any], prospect_id: str):
        prospect_id = _normalize_id(prospect_id, 'prospect id')
        result = self._request(token, 'GET', f"/prospects/{prospect_id}")
        return self._slim(result['data'], PROSPECT_SUMMARY)

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Outreach error (create_prospect)", retry_on=(RetryableApiError,))
    def create_prospect(
        self, *,
        token: OutreachToken,
        ctx: Dict[str, Any],
        emails: List[str],
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        title: Optional[str] = None,
        company: Optional[str] = None,
        tags: Optional[List[str]] = None,
        account_id: Optional[str] = None,
        owner_id: Optional[str] = None,
        extra_attributes: Optional[Dict[str, Any]] = None
    ):
        attributes, relationships = _prospect_payload(
            emails=emails, first_name=first_name, last_name=last_name, title=title,
            company=company, tags=tags, account_id=account_id, owner_id=owner_id,
            extra_attributes=extra_attributes
        )
        return self._create(token, '/prospects', 'prospect', attributes, relationships, PROSPECT_SUMMARY)

    @tool_scope_factory(scopes=SCOPES)
    def create_prospects(
        self, *,
        token: OutreachToken,
        ctx: Dict[str, Any],
        prospects: List[Dict[str, Any]]
    ):
        """
        Serial batch create (Outreach has no bulk endpoint). Failures don't stop the
        batch; each is reported with its index. No whole-method retry — a retry after
        partial success would duplicate records.
        """
        _validate_batch(prospects, 'prospects')
        created, failed = [], []
        for index, item in enumerate(prospects):
            if index:
                time.sleep(BATCH_PACING_SECONDS)
            try:
                attributes, relationships = _prospect_payload(
                    emails=item.get('emails'),
                    first_name=item.get('first_name'),
                    last_name=item.get('last_name'),
                    title=item.get('title'),
                    company=item.get('company'),
                    tags=item.get('tags'),
                    account_id=item.get('account_id'),
                    owner_id=item.get('owner_id'),
                    extra_attributes=item.get('extra_attributes')
                )
                record = self._create(token, '/prospects', 'prospect', attributes, relationships, PROSPECT_SUMMARY)
                created.append({'index': index, **record})
            except (ApiRequestError, RetryableApiError) as e:
                failed.append({'index': index, 'error': str(e)[:500]})

        return {
            'requested': len(prospects),
            'created_count': len(created),
            'failed_count': len(failed),
            'created': created,
            'failed': failed
        }

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Outreach error (update_prospect)", retry_on=(RetryableApiError,))
    def update_prospect(
        self, *,
        token: OutreachToken,
        ctx: Dict[str, Any],
        prospect_id: str,
        attributes: Optional[Dict[str, Any]] = None,
        account_id: Optional[str] = None,
        owner_id: Optional[str] = None
    ):
        prospect_id = _normalize_id(prospect_id, 'prospect id')
        data: Dict[str, Any] = {'type': 'prospect', 'id': prospect_id}
        if attributes:
            data['attributes'] = attributes
        relationships = {}
        if account_id is not None:
            relationships['account'] = _relationship('account', account_id)
        if owner_id is not None:
            relationships['owner'] = _relationship('user', owner_id)
        if relationships:
            data['relationships'] = relationships
        if 'attributes' not in data and 'relationships' not in data:
            raise ApiRequestError("update_prospect requires attributes and/or account_id/owner_id")

        result = self._request(token, 'PATCH', f"/prospects/{prospect_id}", json_body={'data': data})
        return self._slim(result['data'], PROSPECT_SUMMARY)

    # --- accounts ---

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Outreach error (create_account)", retry_on=(RetryableApiError,))
    def create_account(
        self, *,
        token: OutreachToken,
        ctx: Dict[str, Any],
        name: str,
        domain: Optional[str] = None,
        website_url: Optional[str] = None,
        extra_attributes: Optional[Dict[str, Any]] = None
    ):
        attributes = {
            'name': name,
            'domain': domain,
            'websiteUrl': website_url,
            **(extra_attributes or {})
        }
        return self._create(token, '/accounts', 'account', attributes, None, ACCOUNT_SUMMARY)

    # --- sequences ---

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Outreach error (list_sequences)", retry_on=(RetryableApiError,))
    def list_sequences(
        self, *,
        token: OutreachToken,
        ctx: Dict[str, Any],
        name: Optional[str] = None,
        limit: int = 25
    ):
        filters = {'filter[name]': name} if name else {}
        return self._list(token, '/sequences', filters, SEQUENCE_SUMMARY, limit)

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Outreach error (create_sequence)", retry_on=(RetryableApiError,))
    def create_sequence(
        self, *,
        token: OutreachToken,
        ctx: Dict[str, Any],
        name: str,
        description: Optional[str] = None,
        share_type: str = 'private'
    ):
        if share_type not in SHARE_TYPES:
            raise ApiRequestError(f"share_type must be one of {sorted(SHARE_TYPES)}")

        attributes = {'name': name, 'description': description, 'shareType': share_type}
        return self._create(token, '/sequences', 'sequence', attributes, None, SEQUENCE_SUMMARY)

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Outreach error (create_sequence_step)", retry_on=(RetryableApiError,))
    def create_sequence_step(
        self, *,
        token: OutreachToken,
        ctx: Dict[str, Any],
        sequence_id: str,
        step_type: str,
        order: Optional[int] = None,
        interval_seconds: Optional[int] = None,
        task_note: Optional[str] = None
    ):
        if step_type not in STEP_TYPES:
            raise ApiRequestError(f"step_type must be one of {sorted(STEP_TYPES)}")

        attributes = {
            'stepType': step_type,
            'order': order,
            'interval': interval_seconds,
            'taskNote': task_note
        }
        relationships = {'sequence': _relationship('sequence', sequence_id)}
        return self._create(
            token, '/sequenceSteps', 'sequenceStep', attributes, relationships, SEQUENCE_STEP_SUMMARY
        )

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Outreach error (add_prospect_to_sequence)", retry_on=(RetryableApiError,))
    def add_prospect_to_sequence(
        self, *,
        token: OutreachToken,
        ctx: Dict[str, Any],
        sequence_id: str,
        prospect_id: str,
        mailbox_id: Optional[str] = None
    ):
        relationships = {
            'sequence': _relationship('sequence', sequence_id),
            'prospect': _relationship('prospect', prospect_id)
        }
        if mailbox_id is not None:
            relationships['mailbox'] = _relationship('mailbox', mailbox_id)

        return self._create(
            token, '/sequenceStates', 'sequenceState', {}, relationships, SEQUENCE_STATE_SUMMARY
        )

    @tool_scope_factory(scopes=SCOPES)
    def enroll_prospects(
        self, *,
        token: OutreachToken,
        ctx: Dict[str, Any],
        sequence_id: str,
        prospect_ids: List[str],
        mailbox_id: str
    ):
        """
        Serial batch enrollment into one sequence. Outreach requires a mailbox on every
        sequenceState, so it's a required argument here.
        """
        _validate_batch(prospect_ids, 'prospect_ids')
        sequence_rel = _relationship('sequence', sequence_id)
        mailbox_rel = _relationship('mailbox', mailbox_id)
        created, failed = [], []
        for index, prospect_id in enumerate(prospect_ids):
            if index:
                time.sleep(BATCH_PACING_SECONDS)
            try:
                relationships = {
                    'sequence': sequence_rel,
                    'prospect': _relationship('prospect', prospect_id),
                    'mailbox': mailbox_rel
                }
                record = self._create(
                    token, '/sequenceStates', 'sequenceState', {}, relationships, SEQUENCE_STATE_SUMMARY
                )
                created.append({'index': index, 'prospect_id': prospect_id, **record})
            except (ApiRequestError, RetryableApiError) as e:
                failed.append({'index': index, 'prospect_id': prospect_id, 'error': str(e)[:500]})

        return {
            'requested': len(prospect_ids),
            'created_count': len(created),
            'failed_count': len(failed),
            'created': created,
            'failed': failed
        }

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Outreach error (activate_sequence)", retry_on=(RetryableApiError,))
    def activate_sequence(self, *, token: OutreachToken, ctx: Dict[str, Any], sequence_id: str):
        """
        Enable a sequence via the actions sub-endpoint. PATCHing 'enabled' is rejected
        ("private attribute"); POST /sequences/{id}/actions/activate is the supported
        path (mirrors the Nooks apiWrappers/outreach activateSequence).
        """
        sequence_id = _normalize_id(sequence_id, 'sequence id')
        self._request(token, 'POST', f"/sequences/{sequence_id}/actions/activate")
        result = self._request(token, 'GET', f"/sequences/{sequence_id}")
        return self._slim(result['data'], SEQUENCE_SUMMARY)

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Outreach error (create_template)", retry_on=(RetryableApiError,))
    def create_template(
        self, *,
        token: OutreachToken,
        ctx: Dict[str, Any],
        name: str,
        subject: str,
        body_html: str,
        share_type: str = 'shared',
        tags: Optional[List[str]] = None,
        track_links: bool = True,
        track_opens: bool = True,
        owner_id: Optional[str] = None
    ):
        if share_type not in SHARE_TYPES:
            raise ApiRequestError(f"share_type must be one of {sorted(SHARE_TYPES)}")

        attributes = {
            'name': name,
            'subject': subject,
            'bodyHtml': body_html,
            'shareType': share_type,
            'tags': tags,
            'trackLinks': track_links,
            'trackOpens': track_opens
        }
        relationships = {}
        if owner_id is not None:
            relationships['owner'] = _relationship('user', owner_id)

        return self._create(token, '/templates', 'template', attributes, relationships, TEMPLATE_SUMMARY)

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Outreach error (link_template_to_step)", retry_on=(RetryableApiError,))
    def link_template_to_step(
        self, *,
        token: OutreachToken,
        ctx: Dict[str, Any],
        sequence_step_id: str,
        template_id: str,
        is_reply: bool = False,
        activate: bool = True
    ):
        relationships = {
            'sequenceStep': _relationship('sequenceStep', sequence_step_id),
            'template': _relationship('template', template_id)
        }
        record = self._create(
            token, '/sequenceTemplates', 'sequenceTemplate',
            {'isReply': is_reply}, relationships, SEQUENCE_TEMPLATE_SUMMARY
        )
        if activate:
            self._request(token, 'POST', f"/sequenceTemplates/{record['id']}/actions/activate")
            record['activated'] = True
        return record

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Outreach error (update_sequence)", retry_on=(RetryableApiError,))
    def update_sequence(
        self, *,
        token: OutreachToken,
        ctx: Dict[str, Any],
        sequence_id: str,
        attributes: Dict[str, Any]
    ):
        sequence_id = _normalize_id(sequence_id, 'sequence id')
        if not attributes:
            raise ApiRequestError("update_sequence requires at least one attribute")

        data = {'type': 'sequence', 'id': sequence_id, 'attributes': attributes}
        result = self._request(token, 'PATCH', f"/sequences/{sequence_id}", json_body={'data': data})
        return self._slim(result['data'], SEQUENCE_SUMMARY)

    # --- tasks ---

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Outreach error (create_task)", retry_on=(RetryableApiError,))
    def create_task(
        self, *,
        token: OutreachToken,
        ctx: Dict[str, Any],
        prospect_id: str,
        action: str = 'action_item',
        note: Optional[str] = None,
        due_at: Optional[str] = None,
        owner_id: Optional[str] = None
    ):
        attributes, relationships = _task_payload(
            prospect_id=prospect_id, action=action, note=note, due_at=due_at, owner_id=owner_id
        )
        return self._create(token, '/tasks', 'task', attributes, relationships, TASK_SUMMARY)

    @tool_scope_factory(scopes=SCOPES)
    def create_tasks(
        self, *,
        token: OutreachToken,
        ctx: Dict[str, Any],
        tasks: List[Dict[str, Any]],
        default_owner_id: Optional[str] = None
    ):
        """
        Serial batch create of one-off tasks. Outreach requires an owner on every task;
        default_owner_id fills in for items that don't set their own.
        """
        _validate_batch(tasks, 'tasks')
        created, failed = [], []
        for index, item in enumerate(tasks):
            if index:
                time.sleep(BATCH_PACING_SECONDS)
            try:
                attributes, relationships = _task_payload(
                    prospect_id=item.get('prospect_id'),
                    action=item.get('action', 'action_item'),
                    note=item.get('note'),
                    due_at=item.get('due_at'),
                    owner_id=item.get('owner_id', default_owner_id)
                )
                record = self._create(token, '/tasks', 'task', attributes, relationships, TASK_SUMMARY)
                created.append({'index': index, **record})
            except (ApiRequestError, RetryableApiError) as e:
                failed.append({'index': index, 'error': str(e)[:500]})

        return {
            'requested': len(tasks),
            'created_count': len(created),
            'failed_count': len(failed),
            'created': created,
            'failed': failed
        }

    # --- lookups ---

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Outreach error (list_mailboxes)", retry_on=(RetryableApiError,))
    def list_mailboxes(self, *, token: OutreachToken, ctx: Dict[str, Any], limit: int = 25):
        return self._list(token, '/mailboxes', {}, MAILBOX_SUMMARY, limit)

    @tool_scope_factory(scopes=SCOPES)
    @tool_retry_factory(error_message="Outreach error (list_users)", retry_on=(RetryableApiError,))
    def list_users(
        self, *,
        token: OutreachToken,
        ctx: Dict[str, Any],
        email: Optional[str] = None,
        limit: int = 25
    ):
        filters = {'filter[email]': email} if email else {}
        return self._list(token, '/users', filters, USER_SUMMARY, limit)


def main():
    pass
