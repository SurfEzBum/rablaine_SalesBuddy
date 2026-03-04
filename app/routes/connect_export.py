"""
Connect Export routes for NoteHelper.

Provides functionality to export call log data for writing Microsoft Connects
(self-evaluations). Generates structured summaries and JSON exports scoped to
a configurable date range, with milestone revenue impact per customer.
"""
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from flask import (
    Blueprint, Response, g, jsonify, redirect, render_template, request,
    url_for, flash,
)

from app.models import (
    CallLog, ConnectExport, Customer, Milestone, db,
)

connect_export_bp = Blueprint('connect_export', __name__)

# HTML tag stripper for plain-text output
_TAG_RE = re.compile(r'<[^>]+>')


def _strip_html(html_text: str) -> str:
    """Remove HTML tags and collapse whitespace for plain-text output."""
    if not html_text:
        return ''
    text = _TAG_RE.sub('', html_text)
    # Collapse multiple newlines/spaces
    lines = [line.strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def _build_export_data(user_id: int, start_date: date, end_date: date) -> dict[str, Any]:
    """
    Query all call logs in the date range and build structured export data.

    Returns a dict with:
        - summary: aggregate counts and topic/customer breakdowns
        - customers: per-customer detail with call logs, topics, milestone revenue
    """
    from sqlalchemy import func
    from sqlalchemy.orm import joinedload

    # Convert dates to datetime range for query (inclusive of end_date)
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())

    # Query call logs in range with eager loading
    call_logs = (
        CallLog.query
        .filter(
            CallLog.user_id == user_id,
            CallLog.call_date >= start_dt,
            CallLog.call_date <= end_dt,
        )
        .options(
            joinedload(CallLog.customer).joinedload(Customer.seller),
            joinedload(CallLog.customer).joinedload(Customer.territory),
            joinedload(CallLog.topics),
            joinedload(CallLog.milestones),
        )
        .order_by(CallLog.call_date.asc())
        .all()
    )

    # Group by customer
    customers_map: dict[int, dict] = {}
    topic_counts: dict[str, dict] = {}  # topic_name -> {count, customers set}

    for cl in call_logs:
        cust = cl.customer
        if not cust:
            continue

        cust_id = cust.id
        if cust_id not in customers_map:
            customers_map[cust_id] = {
                'id': cust.id,
                'name': cust.get_display_name(),
                'tpid': cust.tpid,
                'seller': cust.seller.name if cust.seller else None,
                'territory': cust.territory.name if cust.territory else None,
                'call_logs': [],
                'topics': set(),
                'milestone_revenue': 0.0,
                'milestone_count': 0,
            }

        # Add call log
        topics_list = [t.name for t in cl.topics]
        customers_map[cust_id]['call_logs'].append({
            'id': cl.id,
            'date': cl.call_date.strftime('%Y-%m-%d'),
            'content': cl.content,
            'content_text': _strip_html(cl.content),
            'topics': topics_list,
        })
        customers_map[cust_id]['topics'].update(topics_list)

        # Track topic counts
        for topic_name in topics_list:
            if topic_name not in topic_counts:
                topic_counts[topic_name] = {'count': 0, 'customers': set()}
            topic_counts[topic_name]['count'] += 1
            topic_counts[topic_name]['customers'].add(cust.get_display_name())

    # Get milestone revenue per customer (completed milestones where user is on team)
    for cust_id, cust_data in customers_map.items():
        completed_milestones = (
            Milestone.query
            .filter(
                Milestone.customer_id == cust_id,
                Milestone.on_my_team == True,
                Milestone.msx_status == 'Completed',
            )
            .all()
        )
        # Filter to milestones that were updated in the period
        for ms in completed_milestones:
            if ms.updated_at and start_dt <= ms.updated_at <= end_dt:
                cust_data['milestone_revenue'] += ms.dollar_value or 0
                cust_data['milestone_count'] += 1

    # Convert sets to sorted lists for serialization
    for cust_data in customers_map.values():
        cust_data['topics'] = sorted(cust_data['topics'])

    # Sort customers by call log count descending
    customers_list = sorted(
        customers_map.values(),
        key=lambda c: len(c['call_logs']),
        reverse=True,
    )

    # Build topic summary (sorted by count descending)
    topic_summary = [
        {
            'name': name,
            'call_count': data['count'],
            'customer_count': len(data['customers']),
            'customers': sorted(data['customers']),
        }
        for name, data in sorted(
            topic_counts.items(), key=lambda x: x[1]['count'], reverse=True
        )
    ]

    # Total milestone revenue
    total_milestone_revenue = sum(c['milestone_revenue'] for c in customers_list)
    total_milestone_count = sum(c['milestone_count'] for c in customers_list)

    # Unique customers and topics
    unique_customer_count = len(customers_list)
    unique_topic_count = len(topic_summary)

    summary = {
        'start_date': start_date.isoformat(),
        'end_date': end_date.isoformat(),
        'total_call_logs': len(call_logs),
        'unique_customers': unique_customer_count,
        'unique_topics': unique_topic_count,
        'total_milestone_revenue': total_milestone_revenue,
        'total_milestone_count': total_milestone_count,
        'topics': topic_summary,
    }

    return {
        'summary': summary,
        'customers': customers_list,
    }


def _format_currency(amount: float) -> str:
    """Format a dollar amount with commas and no cents."""
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:,.1f}M"
    elif amount >= 1_000:
        return f"${amount / 1_000:,.1f}K"
    else:
        return f"${amount:,.0f}"


def _build_text_export(data: dict, name: str) -> str:
    """Build a copy-pastable plain-text export from structured data."""
    summary = data['summary']
    customers = data['customers']

    lines = []
    lines.append(f"{'=' * 60}")
    lines.append(f"{name}")
    lines.append(f"Period: {summary['start_date']} to {summary['end_date']}")
    lines.append(f"{'=' * 60}")
    lines.append("")
    lines.append(f"{summary['total_call_logs']} call logs across "
                 f"{summary['unique_customers']} customers")

    if summary['total_milestone_revenue'] > 0:
        lines.append(
            f"Influenced {_format_currency(summary['total_milestone_revenue'])} "
            f"of committed milestone revenue "
            f"({summary['total_milestone_count']} milestones)"
        )

    # Topic summary
    if summary['topics']:
        lines.append("")
        lines.append(f"--- Topics ({summary['unique_topics']}) ---")
        for topic in summary['topics']:
            customer_names = ', '.join(topic['customers'][:5])
            suffix = f", +{len(topic['customers']) - 5} more" if len(topic['customers']) > 5 else ""
            lines.append(
                f"  {topic['name']} ({topic['call_count']} calls, "
                f"{topic['customer_count']} customers): {customer_names}{suffix}"
            )

    # Per-customer detail
    lines.append("")
    lines.append(f"{'=' * 60}")
    lines.append("CUSTOMER DETAIL")
    lines.append(f"{'=' * 60}")

    for cust in customers:
        lines.append("")
        lines.append(f"--- {cust['name']} ({len(cust['call_logs'])} call logs) ---")
        if cust['seller']:
            lines.append(f"Seller: {cust['seller']}")
        if cust['territory']:
            lines.append(f"Territory: {cust['territory']}")
        if cust['topics']:
            lines.append(f"Topics: {', '.join(cust['topics'])}")
        if cust['milestone_revenue'] > 0:
            lines.append(
                f"Influenced {_format_currency(cust['milestone_revenue'])} "
                f"of committed milestone revenue "
                f"({cust['milestone_count']} milestones)"
            )
        lines.append("")

        for cl in cust['call_logs']:
            topic_str = f" [{', '.join(cl['topics'])}]" if cl['topics'] else ""
            lines.append(f"  [{cl['date']}]{topic_str}")
            # Indent call log content
            for content_line in cl['content_text'].splitlines():
                lines.append(f"    {content_line}")
            lines.append("")

    return '\n'.join(lines)


def _build_json_export(data: dict, name: str) -> dict:
    """Build the JSON export structure (summary + full customer detail)."""
    return {
        'export_name': name,
        'exported_at': datetime.now(timezone.utc).isoformat(),
        'summary': data['summary'],
        'customers': data['customers'],
    }


def _build_markdown_export(data: dict, name: str) -> str:
    """Build a Markdown-formatted export from structured data."""
    summary = data['summary']
    customers = data['customers']

    lines = []
    lines.append(f"# {name}")
    lines.append(f"**Period:** {summary['start_date']} to {summary['end_date']}")
    lines.append("")
    lines.append(f"{summary['total_call_logs']} call logs across "
                 f"{summary['unique_customers']} customers")

    if summary['total_milestone_revenue'] > 0:
        lines.append(
            f"Influenced **{_format_currency(summary['total_milestone_revenue'])}** "
            f"of committed milestone revenue "
            f"({summary['total_milestone_count']} milestones)"
        )

    # Topic summary
    if summary['topics']:
        lines.append("")
        lines.append(f"## Topics ({summary['unique_topics']})")
        lines.append("")
        for topic in summary['topics']:
            customer_names = ', '.join(topic['customers'][:5])
            suffix = f", +{len(topic['customers']) - 5} more" if len(topic['customers']) > 5 else ""
            lines.append(
                f"- **{topic['name']}** ({topic['call_count']} calls, "
                f"{topic['customer_count']} customers): {customer_names}{suffix}"
            )

    # Per-customer detail
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Customer Detail")

    for cust in customers:
        lines.append("")
        lines.append(f"### {cust['name']} ({len(cust['call_logs'])} call logs)")
        lines.append("")
        meta_parts = []
        if cust['seller']:
            meta_parts.append(f"**Seller:** {cust['seller']}")
        if cust['territory']:
            meta_parts.append(f"**Territory:** {cust['territory']}")
        if cust['topics']:
            meta_parts.append(f"**Topics:** {', '.join(cust['topics'])}")
        if meta_parts:
            lines.append(' | '.join(meta_parts))
            lines.append("")
        if cust['milestone_revenue'] > 0:
            lines.append(
                f"Influenced **{_format_currency(cust['milestone_revenue'])}** "
                f"of committed milestone revenue "
                f"({cust['milestone_count']} milestones)"
            )
            lines.append("")

        for cl in cust['call_logs']:
            topic_str = f" *[{', '.join(cl['topics'])}]*" if cl['topics'] else ""
            lines.append(f"**{cl['date']}**{topic_str}")
            lines.append("")
            lines.append(cl['content_text'])
            lines.append("")

    return '\n'.join(lines)


@connect_export_bp.route('/connect-export')
def connect_export_page():
    """Render the Connect Export page with date picker and previous exports."""
    user = g.user

    # Get previous exports for this user (most recent first)
    previous_exports = (
        ConnectExport.query
        .filter_by(user_id=user.id)
        .order_by(ConnectExport.created_at.desc())
        .all()
    )

    # Auto-populate dates: start = day after last export's end_date, end = today
    default_start = None
    default_end = date.today()

    if previous_exports:
        last_export = previous_exports[0]
        default_start = last_export.end_date + timedelta(days=1)

    return render_template(
        'connect_export.html',
        previous_exports=previous_exports,
        default_start=default_start.isoformat() if default_start else '',
        default_end=default_end.isoformat(),
    )


@connect_export_bp.route('/api/connect-export/generate', methods=['POST'])
def generate_connect_export():
    """
    Generate a Connect export for the given date range.

    Expected JSON body:
        name: string (export name, e.g. "FY26 Final Connect")
        start_date: string (YYYY-MM-DD)
        end_date: string (YYYY-MM-DD)

    Returns JSON with summary and text/json export data.
    """
    if not request.is_json:
        return jsonify({'success': False, 'error': 'JSON body required'}), 400

    user = g.user
    name = request.json.get('name', '').strip()
    start_str = request.json.get('start_date', '').strip()
    end_str = request.json.get('end_date', '').strip()

    if not name:
        return jsonify({'success': False, 'error': 'Export name is required'}), 400
    if not start_str or not end_str:
        return jsonify({'success': False, 'error': 'Start and end dates are required'}), 400

    try:
        start_date = date.fromisoformat(start_str)
        end_date = date.fromisoformat(end_str)
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid date format (use YYYY-MM-DD)'}), 400

    if start_date > end_date:
        return jsonify({'success': False, 'error': 'Start date must be before end date'}), 400

    # Build the export data
    data = _build_export_data(user.id, start_date, end_date)

    # Generate all formats
    text_export = _build_text_export(data, name)
    json_export = _build_json_export(data, name)
    markdown_export = _build_markdown_export(data, name)

    # Save the export record
    export_record = ConnectExport(
        name=name,
        start_date=start_date,
        end_date=end_date,
        call_log_count=data['summary']['total_call_logs'],
        customer_count=data['summary']['unique_customers'],
        user_id=user.id,
    )
    db.session.add(export_record)
    db.session.commit()

    return jsonify({
        'success': True,
        'export_id': export_record.id,
        'summary': data['summary'],
        'text_export': text_export,
        'json_export': json_export,
        'markdown_export': markdown_export,
    })


@connect_export_bp.route('/api/connect-export/<int:export_id>/view')
def view_connect_export(export_id: int):
    """View a previously generated Connect export (regenerates data from saved date range)."""
    user = g.user
    export_record = ConnectExport.query.filter_by(
        id=export_id, user_id=user.id
    ).first()

    if not export_record:
        return jsonify({'success': False, 'error': 'Export not found'}), 404

    # Regenerate the data from the stored date range
    data = _build_export_data(user.id, export_record.start_date, export_record.end_date)
    text_export = _build_text_export(data, export_record.name)
    json_export = _build_json_export(data, export_record.name)
    markdown_export = _build_markdown_export(data, export_record.name)

    return jsonify({
        'success': True,
        'export_id': export_record.id,
        'name': export_record.name,
        'summary': data['summary'],
        'text_export': text_export,
        'json_export': json_export,
        'markdown_export': markdown_export,
    })


@connect_export_bp.route('/api/connect-export/<int:export_id>', methods=['DELETE'])
def delete_connect_export(export_id: int):
    """Delete a Connect export record."""
    user = g.user
    export_record = ConnectExport.query.filter_by(
        id=export_id, user_id=user.id
    ).first()

    if not export_record:
        return jsonify({'success': False, 'error': 'Export not found'}), 404

    db.session.delete(export_record)
    db.session.commit()

    return jsonify({'success': True})
