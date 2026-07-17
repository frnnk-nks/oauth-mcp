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
def outreach_create_sequence_step(
    ctx: Context,
    sequence_id: str,
    step_type: str,
    order: Optional[int] = None,
    interval_minutes: Optional[int] = None,
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
        interval_minutes: Optional delay in minutes after the previous step (or sequence start)
        task_note: Optional note shown on the created task (task/call steps)

    Note: email steps additionally need a template linked via a sequenceTemplate before
    the sequence can be enabled — prefer 'task'/'call' steps for quick fixtures.
    """
    return outreach_tools.run_method(
        'create_sequence_step', ctx=ctx,
        sequence_id=sequence_id, step_type=step_type, order=order,
        interval_minutes=interval_minutes, task_note=task_note
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
