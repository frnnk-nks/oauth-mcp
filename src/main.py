"""
FastMCP server, aggregating all resources, tools, and prompts.
"""

import os
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Context
from mcp_tools.salesforce.salesforce import SalesforceToolApp
from mcp_tools.outreach.outreach import OutreachToolApp
from auth.providers.provider_registry import get_provider
from auth.oauth_gate import elicitation_mapping, callback_state
from utils.decorators import mcp_oauth_handler
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse

load_dotenv()
SERVER_HOST = os.getenv('SERVER_HOST')
SERVER_PORT = os.getenv('SERVER_PORT')
SERVER_ORIGIN_PROXY = os.getenv('SERVER_ORIGIN_PROXY')

SALESFORCE_AUTH_MESSAGE = "Authorization is required to access your Salesforce org."
OUTREACH_AUTH_MESSAGE = "Authorization is required to access your Outreach workspace."

mcp = FastMCP(name="crm-sep-sandbox-mcp", host=SERVER_HOST, port=SERVER_PORT)
salesforce_tools = SalesforceToolApp(provider=get_provider('salesforce'))
outreach_tools = OutreachToolApp(provider=get_provider('outreach'))


@mcp.custom_route("/auth/connect/{elicitation_id}", methods=['GET'])
async def auth_connect(request: Request) -> PlainTextResponse:
    elicitation_id = request.path_params['elicitation_id']
    elicitation_body = elicitation_mapping[elicitation_id]

    origin = SERVER_ORIGIN_PROXY if SERVER_ORIGIN_PROXY else f"http://{SERVER_HOST}:{SERVER_PORT}"
    provider = get_provider(provider_name=elicitation_body['provider_name'])
    provider_state = provider.generate_auth_url(
        scopes=elicitation_body['scopes'],
        elicitation_id=elicitation_id,
        proxy_origin=origin
    )
    callback_state[elicitation_id] = provider_state

    return RedirectResponse(url=provider_state['auth_url'])


@mcp.custom_route("/auth/callback", methods=['GET'])
async def auth_callback(request: Request) -> PlainTextResponse:
    elicitation_id = request.query_params.get('state')
    provider_state = callback_state[elicitation_id]
    uri = str(request.url)

    provider = get_provider(provider_name=provider_state['provider'])
    provider.finish_auth(provider_state=provider_state, uri=uri)

    return PlainTextResponse("You may close this tab.")


# --- Salesforce tools ---
# Object mapping: Account -> Nooks account, Contact -> account-linked prospect,
# Lead -> standalone prospect (Company instead of AccountId).

@mcp.tool()
@mcp_oauth_handler(SALESFORCE_AUTH_MESSAGE)
def salesforce_query(ctx: Context, soql: str, max_pages: int = 10):
    """
    Run a SOQL query against the connected Salesforce org.

    Args:
        soql: Full SOQL statement, e.g.
            "SELECT Id, Name, Email FROM Contact WHERE Email IN ('a@x.com')".
            Escape single quotes inside string literals with a backslash.
        max_pages: Maximum result pages to follow (default: 10).

    Returns total_size and records. Record Id values are usable with the other
    salesforce tools (get/update).
    """
    return salesforce_tools.run_method('soql_query', ctx=ctx, soql=soql, max_pages=max_pages)


@mcp.tool()
@mcp_oauth_handler(SALESFORCE_AUTH_MESSAGE)
def salesforce_get_record(
    ctx: Context,
    sobject: str,
    record_id: str,
    fields: Optional[List[str]] = None
):
    """
    Fetch one Salesforce record by ID.

    Args:
        sobject: Object API name, e.g. 'Account', 'Contact', 'Lead'
        record_id: 15- or 18-character Salesforce record ID
        fields: Optional list of field API names to return (all fields if omitted)
    """
    return salesforce_tools.run_method(
        'get_record', ctx=ctx, sobject=sobject, record_id=record_id, fields=fields
    )


@mcp.tool()
@mcp_oauth_handler(SALESFORCE_AUTH_MESSAGE)
def salesforce_create_account(
    ctx: Context,
    name: str,
    website: Optional[str] = None,
    extra_fields: Optional[Dict[str, Any]] = None
):
    """
    Create a Salesforce Account (maps to a Nooks account). Create the Account first,
    then link Contacts to it via account_id.

    Args:
        name: Account name (required by Salesforce)
        website: Optional website URL
        extra_fields: Optional additional field API name -> value pairs (e.g. custom fields)
    """
    fields: Dict[str, Any] = {'Name': name}
    if website is not None:
        fields['Website'] = website
    if extra_fields:
        fields.update(extra_fields)
    return salesforce_tools.run_method('create_record', ctx=ctx, sobject='Account', fields=fields)


@mcp.tool()
@mcp_oauth_handler(SALESFORCE_AUTH_MESSAGE)
def salesforce_create_contact(
    ctx: Context,
    last_name: str,
    first_name: Optional[str] = None,
    email: Optional[str] = None,
    account_id: Optional[str] = None,
    title: Optional[str] = None,
    phone: Optional[str] = None,
    extra_fields: Optional[Dict[str, Any]] = None
):
    """
    Create a Salesforce Contact — an account-linked prospect in Nooks terms.

    Prerequisites:
        - account_id: Obtain from salesforce_create_account or salesforce_query.
          A Contact without an AccountId is a private contact — usually not what you want.

    Args:
        last_name: Contact last name (required by Salesforce)
        first_name: Optional first name
        email: Optional email address
        account_id: Salesforce Account ID to link the Contact to
        title: Optional job title
        phone: Optional phone number
        extra_fields: Optional additional field API name -> value pairs
    """
    fields: Dict[str, Any] = {'LastName': last_name}
    if first_name is not None:
        fields['FirstName'] = first_name
    if email is not None:
        fields['Email'] = email
    if account_id is not None:
        fields['AccountId'] = account_id
    if title is not None:
        fields['Title'] = title
    if phone is not None:
        fields['Phone'] = phone
    if extra_fields:
        fields.update(extra_fields)
    return salesforce_tools.run_method('create_record', ctx=ctx, sobject='Contact', fields=fields)


@mcp.tool()
@mcp_oauth_handler(SALESFORCE_AUTH_MESSAGE)
def salesforce_create_lead(
    ctx: Context,
    last_name: str,
    company: str,
    first_name: Optional[str] = None,
    email: Optional[str] = None,
    title: Optional[str] = None,
    phone: Optional[str] = None,
    extra_fields: Optional[Dict[str, Any]] = None
):
    """
    Create a Salesforce Lead — a standalone prospect in Nooks terms. Leads are not
    linked to Accounts; they carry a Company name instead.

    Args:
        last_name: Lead last name (required by Salesforce)
        company: Company name (required by Salesforce)
        first_name: Optional first name
        email: Optional email address
        title: Optional job title
        phone: Optional phone number
        extra_fields: Optional additional field API name -> value pairs
    """
    fields: Dict[str, Any] = {'LastName': last_name, 'Company': company}
    if first_name is not None:
        fields['FirstName'] = first_name
    if email is not None:
        fields['Email'] = email
    if title is not None:
        fields['Title'] = title
    if phone is not None:
        fields['Phone'] = phone
    if extra_fields:
        fields.update(extra_fields)
    return salesforce_tools.run_method('create_record', ctx=ctx, sobject='Lead', fields=fields)


@mcp.tool()
@mcp_oauth_handler(SALESFORCE_AUTH_MESSAGE)
def salesforce_update_record(
    ctx: Context,
    sobject: str,
    record_id: str,
    fields: Dict[str, Any]
):
    """
    Update fields on an existing Salesforce record (e.g. modify prospect data on a
    Contact or Lead).

    Prerequisites:
        - record_id: Obtain from salesforce_query or a salesforce_create_* tool.

    Args:
        sobject: Object API name, e.g. 'Account', 'Contact', 'Lead'
        record_id: 15- or 18-character Salesforce record ID
        fields: Field API name -> new value pairs, e.g. {"Title": "VP Sales"}
    """
    return salesforce_tools.run_method(
        'update_record', ctx=ctx, sobject=sobject, record_id=record_id, fields=fields
    )


@mcp.tool()
@mcp_oauth_handler(SALESFORCE_AUTH_MESSAGE)
def salesforce_create_records(
    ctx: Context,
    records: List[Dict[str, Any]],
    all_or_none: bool = False
):
    """
    Bulk-create up to 1000 Salesforce records of mixed types in one call (composite API,
    200 per request under the hood). Prefer this over repeated salesforce_create_* calls
    for batches.

    Args:
        records: List of {"sobject": "Contact"|"Lead"|"Account"|..., "fields": {...}} items,
            e.g. [{"sobject": "Contact", "fields": {"LastName": "Smith", "AccountId": "001..."}},
                  {"sobject": "Lead", "fields": {"LastName": "Jones", "Company": "Acme"}}]
        all_or_none: If true, any failure rolls back the whole 200-record chunk (default: false)

    Returns created ids and per-index failures. Failed items can be retried in a
    follow-up call using the reported indexes.
    """
    return salesforce_tools.run_method(
        'create_records', ctx=ctx, records=records, all_or_none=all_or_none
    )


# --- Outreach tools ---
# Object mapping: Outreach account -> Nooks account, Outreach prospect -> Nooks prospect.

@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_find_prospects(
    ctx: Context,
    emails: Optional[List[str]] = None,
    prospect_ids: Optional[List[str]] = None,
    account_id: Optional[str] = None,
    limit: int = 50
):
    """
    Find Outreach prospects by email, ID, and/or account. With no filters, lists
    recent prospects.

    Args:
        emails: Optional list of email addresses to match
        prospect_ids: Optional list of Outreach prospect IDs
        account_id: Optional Outreach account ID to filter by
        limit: Max results (default: 50)

    Returns prospect id values needed by the other outreach tools.
    """
    return outreach_tools.run_method(
        'find_prospects', ctx=ctx,
        emails=emails, prospect_ids=prospect_ids, account_id=account_id, limit=limit
    )


@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_get_prospect(ctx: Context, prospect_id: str):
    """
    Fetch one Outreach prospect by ID.

    Args:
        prospect_id: Outreach prospect ID (from outreach_find_prospects or a create)
    """
    return outreach_tools.run_method('get_prospect', ctx=ctx, prospect_id=prospect_id)


@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_create_prospect(
    ctx: Context,
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
    """
    Create an Outreach prospect (maps to a Nooks prospect).

    Prerequisites:
        - account_id: Optional; obtain from outreach_create_account to link the prospect
        - owner_id: Optional Outreach user ID; obtain from outreach_list_users

    Args:
        emails: List of email addresses (at least one)
        first_name: Optional first name
        last_name: Optional last name
        title: Optional job title
        company: Optional company name
        tags: Optional list of tags
        account_id: Optional Outreach account ID to link
        owner_id: Optional Outreach user ID to assign as owner
        extra_attributes: Optional additional Outreach attribute name -> value pairs (camelCase)
    """
    return outreach_tools.run_method(
        'create_prospect', ctx=ctx,
        emails=emails, first_name=first_name, last_name=last_name, title=title,
        company=company, tags=tags, account_id=account_id, owner_id=owner_id,
        extra_attributes=extra_attributes
    )


@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_create_prospects(ctx: Context, prospects: List[Dict[str, Any]]):
    """
    Batch-create up to 100 Outreach prospects in one call (created serially server-side;
    Outreach has no bulk endpoint). Prefer this over repeated outreach_create_prospect
    calls for batches.

    Args:
        prospects: List of items shaped like outreach_create_prospect's arguments:
            {"emails": ["a@x.com"], "first_name"?, "last_name"?, "title"?, "company"?,
             "tags"?: [...], "account_id"?, "owner_id"?, "extra_attributes"?: {...}}

    Returns created prospects and per-index failures; a failure doesn't stop the batch.
    """
    return outreach_tools.run_method('create_prospects', ctx=ctx, prospects=prospects)


@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_update_prospect(
    ctx: Context,
    prospect_id: str,
    attributes: Optional[Dict[str, Any]] = None,
    account_id: Optional[str] = None,
    owner_id: Optional[str] = None
):
    """
    Update an existing Outreach prospect's attributes and/or relationships.

    Args:
        prospect_id: Outreach prospect ID
        attributes: Outreach attribute name -> new value pairs (camelCase),
            e.g. {"title": "VP Sales", "tags": ["qa-fixture"]}
        account_id: Optional Outreach account ID to (re)link
        owner_id: Optional Outreach user ID to assign as owner
    """
    return outreach_tools.run_method(
        'update_prospect', ctx=ctx,
        prospect_id=prospect_id, attributes=attributes,
        account_id=account_id, owner_id=owner_id
    )


@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_create_account(
    ctx: Context,
    name: str,
    domain: Optional[str] = None,
    website_url: Optional[str] = None,
    extra_attributes: Optional[Dict[str, Any]] = None
):
    """
    Create an Outreach account (maps to a Nooks account). Create the account first,
    then link prospects to it via account_id.

    Args:
        name: Account name
        domain: Optional company domain, e.g. 'acme.com'
        website_url: Optional website URL
        extra_attributes: Optional additional Outreach attribute name -> value pairs (camelCase)
    """
    return outreach_tools.run_method(
        'create_account', ctx=ctx,
        name=name, domain=domain, website_url=website_url, extra_attributes=extra_attributes
    )


@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_list_sequences(ctx: Context, name: Optional[str] = None, limit: int = 25):
    """
    List Outreach sequences, optionally filtered by exact name.

    Args:
        name: Optional exact sequence name to match
        limit: Max results (default: 25)

    Returns sequence id values needed for steps and enrollment.
    """
    return outreach_tools.run_method('list_sequences', ctx=ctx, name=name, limit=limit)


@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_create_sequence(
    ctx: Context,
    name: str,
    description: Optional[str] = None,
    share_type: str = 'private'
):
    """
    Create an Outreach sequence. Add steps with outreach_create_sequence_step, then
    enroll prospects with outreach_add_prospect_to_sequence.

    Note: new sequences are DISABLED — enable with outreach_activate_sequence before
    enrolling if steps should actually execute. For Nooks migration-testing fixtures,
    use share_type='shared' and activate (migration discovery only picks up enabled
    sequences visible to the workspace admin token).

    Args:
        name: Sequence name
        description: Optional description
        share_type: 'private' (default), 'read_only', or 'shared'
    """
    return outreach_tools.run_method(
        'create_sequence', ctx=ctx, name=name, description=description, share_type=share_type
    )


@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_update_sequence(
    ctx: Context,
    sequence_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    extra_attributes: Optional[Dict[str, Any]] = None
):
    """
    Update an Outreach sequence's writable attributes (name, description, tags, ...).

    Note: 'enabled' cannot be written here (private attribute) — use
    outreach_activate_sequence to enable a sequence.

    Args:
        sequence_id: Outreach sequence ID
        name: Optional new name
        description: Optional new description
        extra_attributes: Optional additional writable Outreach attribute name -> value
            pairs (camelCase)
    """
    attributes: Dict[str, Any] = dict(extra_attributes or {})
    if name is not None:
        attributes['name'] = name
    if description is not None:
        attributes['description'] = description
    return outreach_tools.run_method(
        'update_sequence', ctx=ctx, sequence_id=sequence_id, attributes=attributes
    )


@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_create_sequence_step(
    ctx: Context,
    sequence_id: str,
    step_type: str,
    order: Optional[int] = None,
    interval_seconds: Optional[int] = None,
    task_note: Optional[str] = None
):
    """
    Add a step to an Outreach sequence. Use step_type 'task' to create a sequence task
    step (task_note becomes the task's note).

    Prerequisites:
        - sequence_id: Obtain from outreach_create_sequence or outreach_list_sequences.

    Args:
        sequence_id: Outreach sequence ID
        step_type: 'auto_email', 'manual_email', 'call', or 'task'
        order: Optional step order within the sequence (1-based)
        interval_seconds: Optional delay in SECONDS after the previous step / sequence
            start (e.g. 300 = 5 minutes; Outreach's interval unit is seconds)
        task_note: Optional note shown on the created task (task/call steps)

    Note: email steps need a template attached before they can send — create one with
    outreach_create_template, then attach via outreach_link_template_to_step. Prefer
    'task'/'call' steps for quick fixtures that skip templates.
    """
    return outreach_tools.run_method(
        'create_sequence_step', ctx=ctx,
        sequence_id=sequence_id, step_type=step_type, order=order,
        interval_seconds=interval_seconds, task_note=task_note
    )


@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_activate_sequence(ctx: Context, sequence_id: str):
    """
    Enable (activate) an Outreach sequence so enrollments and steps actually execute.
    Sequences are created disabled; Nooks migration discovery only considers enabled
    sequences, so migration-testing fixtures should be activated after steps are added.

    Prerequisites:
        - sequence_id: Obtain from outreach_create_sequence or outreach_list_sequences
        - Email steps must have an activated template attached
          (outreach_link_template_to_step) or activation may be rejected.

    Args:
        sequence_id: Outreach sequence ID
    """
    return outreach_tools.run_method('activate_sequence', ctx=ctx, sequence_id=sequence_id)


@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_create_template(
    ctx: Context,
    name: str,
    subject: str,
    body_html: str,
    share_type: str = 'shared',
    tags: Optional[List[str]] = None,
    track_links: bool = True,
    track_opens: bool = True,
    owner_id: Optional[str] = None
):
    """
    Create an Outreach email template, for attaching to email sequence steps.

    Subject/body may contain Outreach variables like {{first_name}} — for Nooks
    migration variable-resolution testing, include non-standard variables
    (e.g. {{sender_name}}) on purpose.

    Args:
        name: Template name
        subject: Email subject (may contain {{variables}})
        body_html: Email body HTML (may contain {{variables}})
        share_type: 'shared' (default; required for migration fixtures), 'private', or 'read_only'
        tags: Optional list of tags
        track_links: Track link clicks (default: true)
        track_opens: Track opens (default: true)
        owner_id: Optional Outreach user ID as owner
    """
    return outreach_tools.run_method(
        'create_template', ctx=ctx,
        name=name, subject=subject, body_html=body_html, share_type=share_type,
        tags=tags, track_links=track_links, track_opens=track_opens, owner_id=owner_id
    )


@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_link_template_to_step(
    ctx: Context,
    sequence_step_id: str,
    template_id: str,
    is_reply: bool = False,
    activate: bool = True
):
    """
    Attach an email template to an email sequence step (creates a sequenceTemplate),
    activating it by default. Email steps can't send without an activated template.

    Prerequisites:
        - sequence_step_id: From outreach_create_sequence_step (an email step type)
        - template_id: From outreach_create_template

    Args:
        sequence_step_id: Outreach sequence step ID
        template_id: Outreach template ID
        is_reply: Whether the template is a reply (threads onto the prior email; default false)
        activate: Activate the sequenceTemplate after linking (default: true)
    """
    return outreach_tools.run_method(
        'link_template_to_step', ctx=ctx,
        sequence_step_id=sequence_step_id, template_id=template_id,
        is_reply=is_reply, activate=activate
    )


@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_add_prospect_to_sequence(
    ctx: Context,
    sequence_id: str,
    prospect_id: str,
    mailbox_id: Optional[str] = None
):
    """
    Enroll a prospect in a sequence (creates an Outreach sequenceState).

    Prerequisites:
        - sequence_id: Obtain from outreach_list_sequences or outreach_create_sequence
        - prospect_id: Obtain from outreach_find_prospects or outreach_create_prospect
        - mailbox_id: Obtain from outreach_list_mailboxes. Outreach rejects enrollment
          without one ("Mailbox can't be blank"), even for sequences with no email steps.

    Args:
        sequence_id: Outreach sequence ID
        prospect_id: Outreach prospect ID
        mailbox_id: Outreach mailbox ID to enroll with (effectively required)
    """
    return outreach_tools.run_method(
        'add_prospect_to_sequence', ctx=ctx,
        sequence_id=sequence_id, prospect_id=prospect_id, mailbox_id=mailbox_id
    )


@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_enroll_prospects(
    ctx: Context,
    sequence_id: str,
    prospect_ids: List[str],
    mailbox_id: str
):
    """
    Batch-enroll up to 100 prospects into one sequence (serial sequenceState creation).

    Prerequisites:
        - sequence_id: Obtain from outreach_list_sequences or outreach_create_sequence
        - prospect_ids: Obtain from outreach_find_prospects or outreach_create_prospects
        - mailbox_id: Obtain from outreach_list_mailboxes (required on every enrollment)

    Returns created sequenceStates and per-index failures; a failure doesn't stop the batch.
    """
    return outreach_tools.run_method(
        'enroll_prospects', ctx=ctx,
        sequence_id=sequence_id, prospect_ids=prospect_ids, mailbox_id=mailbox_id
    )


@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_create_task(
    ctx: Context,
    prospect_id: str,
    action: str = 'action_item',
    note: Optional[str] = None,
    due_at: Optional[str] = None,
    owner_id: Optional[str] = None
):
    """
    Create a one-off Outreach task on a prospect (independent of any sequence).

    Prerequisites:
        - prospect_id: Obtain from outreach_find_prospects or outreach_create_prospect
        - owner_id: Obtain from outreach_list_users. Outreach rejects tasks without an
          owner ("Owner can't be blank").

    Args:
        prospect_id: Outreach prospect ID
        action: Task action, e.g. 'action_item' (default), 'call', 'email'
        note: Optional task note
        due_at: Optional due time in ISO format (e.g. '2026-07-20T09:00:00Z')
        owner_id: Outreach user ID to assign the task to (effectively required)
    """
    return outreach_tools.run_method(
        'create_task', ctx=ctx,
        prospect_id=prospect_id, action=action, note=note, due_at=due_at, owner_id=owner_id
    )


@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_create_tasks(
    ctx: Context,
    tasks: List[Dict[str, Any]],
    default_owner_id: Optional[str] = None
):
    """
    Batch-create up to 100 one-off Outreach tasks (created serially server-side).

    Prerequisites:
        - Every task needs an owner (Outreach rejects ownerless tasks); set per-item
          owner_id or pass default_owner_id from outreach_list_users.

    Args:
        tasks: List of items shaped like outreach_create_task's arguments:
            {"prospect_id": "123", "action"?: "action_item", "note"?, "due_at"?, "owner_id"?}
        default_owner_id: Outreach user ID applied to items without their own owner_id

    Returns created tasks and per-index failures; a failure doesn't stop the batch.
    """
    return outreach_tools.run_method(
        'create_tasks', ctx=ctx, tasks=tasks, default_owner_id=default_owner_id
    )


@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_list_mailboxes(ctx: Context, limit: int = 25):
    """
    List Outreach mailboxes. Returns mailbox id values needed to enroll prospects in
    sequences that contain email steps.

    Args:
        limit: Max results (default: 25)
    """
    return outreach_tools.run_method('list_mailboxes', ctx=ctx, limit=limit)


@mcp.tool()
@mcp_oauth_handler(OUTREACH_AUTH_MESSAGE)
def outreach_list_users(ctx: Context, email: Optional[str] = None, limit: int = 25):
    """
    List Outreach users, optionally filtered by email. Returns user id values usable
    as owner_id in prospect/task tools.

    Args:
        email: Optional exact user email to match
        limit: Max results (default: 25)
    """
    return outreach_tools.run_method('list_users', ctx=ctx, email=email, limit=limit)


@mcp.resource("greeting://{name}")
def get_greeting(name: str) -> str:
    """Get a personalized greeting"""
    return f"Hello, {name}!"


def main():
    """
    Entrypoint for MCP server
    """
    mcp.run(
        transport="streamable-http"
    )


if __name__ == "__main__":
    main()
